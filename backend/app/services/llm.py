"""
Jira Cortex - LLM Service

OpenAI integration with strict hallucination prevention and usage tracking.
"""

from typing import List, Optional, Tuple
from dataclasses import dataclass
import structlog
from openai import AsyncOpenAI, OpenAIError

from app.config import get_settings
from app.services.vector_store import SearchResult

logger = structlog.get_logger(__name__)


@dataclass
class LLMResponse:
    """Response from LLM with usage tracking."""
    answer: str
    confidence_score: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str


@dataclass
class EmbeddingResult:
    """Result from embedding generation with usage tracking."""
    embedding: List[float]
    tokens_used: int
    model: str


class LLMServiceError(Exception):
    """Raised when LLM operations fail."""
    pass


class LLMService:
    """
    OpenAI LLM service with strict hallucination prevention.
    
    Features:
    - Low temperature (0.1) for factual responses
    - Negative prompting to prevent fabrication
    - Confidence score estimation
    - Token usage tracking for billing
    """
    
    # System prompt for RAG with strict guardrails
    SYSTEM_PROMPT = """You are a Senior Technical Lead assistant integrated into Jira. Your role is to help developers find solutions based on historical tickets and documentation.

CRITICAL RULES:
1. Answer ONLY based on the context provided below. Do not use external knowledge.
2. If the answer is not in the context, respond: "I couldn't find relevant information in your accessible projects."
3. Always cite your sources using the format: [Issue-Key: PROJ-123]
4. Be concise and technical.
5. If you find a partial match, clearly state the confidence level.
6. Never make up ticket numbers, solutions, or technical details.

When providing a solution:
- State the solution clearly
- Reference the source ticket(s)
- Mention any caveats or differences from the current issue
"""

    # Prompt for confidence scoring
    CONFIDENCE_PROMPT = """Based on how well the context matches the question, rate your confidence from 0-100:
- 90-100: Direct match, same issue or nearly identical
- 70-89: Strong match, similar issue with applicable solution
- 50-69: Moderate match, related information but may need adaptation
- 30-49: Weak match, tangentially related
- 0-29: No relevant information found
"""

    def __init__(self):
        self.settings = get_settings()
        self._client: Optional[AsyncOpenAI] = None
    
    @property
    def client(self) -> AsyncOpenAI:
        """Lazy initialization of OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                timeout=30.0
            )
        return self._client
    
    async def generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding vector for text.
        
        Args:
            text: Text to embed
            
        Returns:
            Embedding vector (1536 dimensions for text-embedding-3-small)
        """
        result = await self.generate_embedding_with_usage(text)
        return result[0]
    
    async def generate_embedding_with_usage(self, text: str) -> Tuple[List[float], int]:
        """
        Generate embedding vector for text with usage tracking.
        
        Args:
            text: Text to embed
            
        Returns:
            Tuple of (embedding vector, tokens used)
        """
        if not text.strip():
            raise LLMServiceError("Cannot embed empty text")
        
        try:
            response = await self.client.embeddings.create(
                model=self.settings.openai_embedding_model,
                input=text,
                encoding_format="float"
            )
            
            # FIXED: Return token usage for billing
            tokens_used = response.usage.total_tokens if response.usage else 0
            
            logger.debug(
                "embedding_generated",
                tokens=tokens_used,
                model=self.settings.openai_embedding_model
            )
            
            return response.data[0].embedding, tokens_used
            
        except OpenAIError as e:
            logger.error("embedding_failed", error=str(e))
            raise LLMServiceError(f"Embedding generation failed: {e}")
    
    async def generate_embeddings_batch(
        self, 
        texts: List[str]
    ) -> List[List[float]]:
        """
        Generate embeddings for multiple texts in batch.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            List of embedding vectors
        """
        embeddings, _ = await self.generate_embeddings_batch_with_usage(texts)
        return embeddings
    
    async def generate_embeddings_batch_with_usage(
        self, 
        texts: List[str]
    ) -> Tuple[List[List[float]], int]:
        """
        Generate embeddings for multiple texts in batch with usage tracking.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            Tuple of (list of embedding vectors, total tokens used)
        """
        if not texts:
            return [], 0
        
        # Filter empty texts
        valid_texts = [t for t in texts if t.strip()]
        
        if not valid_texts:
            raise LLMServiceError("All texts are empty")
        
        try:
            response = await self.client.embeddings.create(
                model=self.settings.openai_embedding_model,
                input=valid_texts,
                encoding_format="float"
            )
            
            # FIXED: Track token usage for billing
            tokens_used = response.usage.total_tokens if response.usage else 0
            
            logger.info(
                "batch_embeddings_generated",
                count=len(valid_texts),
                tokens=tokens_used,
                model=self.settings.openai_embedding_model
            )
            
            # Sort by index to maintain order
            embeddings = sorted(response.data, key=lambda x: x.index)
            return [e.embedding for e in embeddings], tokens_used
            
        except OpenAIError as e:
            logger.error("batch_embedding_failed", error=str(e))
            raise LLMServiceError(f"Batch embedding failed: {e}")
    
    async def generate_answer(
        self,
        query: str,
        search_results: List[SearchResult],
        additional_context: Optional[str] = None
    ) -> LLMResponse:
        """
        Generate answer using RAG with strict hallucination controls.
        
        Args:
            query: User's question
            search_results: Retrieved documents for context
            additional_context: Optional additional context (e.g., current issue)
            
        Returns:
            LLMResponse with answer and usage stats
        """
        # Build context from search results
        context_parts = []
        
        for i, result in enumerate(search_results, 1):
            context_parts.append(
                f"[Document {i}]\n"
                f"Issue: {result.issue_key}\n"
                f"Title: {result.issue_title}\n"
                f"Relevance: {result.score:.2%}\n"
                f"Content:\n{result.content}\n"
            )
        
        if additional_context:
            context_parts.append(f"\n[Current Issue Context]\n{additional_context}")
        
        context = "\n---\n".join(context_parts)
        
        # Build user prompt
        user_prompt = f"""CONTEXT:
{context if context_parts else "No relevant documents found."}

QUESTION:
{query}

{self.CONFIDENCE_PROMPT}

Provide your answer and confidence score in this format:
CONFIDENCE: [score]
ANSWER: [your answer with citations]"""

        try:
            response = await self.client.chat.completions.create(
                model=self.settings.openai_chat_model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=self.settings.openai_temperature,  # 0.1 for strict
                max_tokens=self.settings.openai_max_tokens,
                top_p=0.9,
                presence_penalty=0.1,
                frequency_penalty=0.1
            )
            
            # Extract response
            content = response.choices[0].message.content or ""
            
            # Parse confidence and answer
            confidence, answer = self._parse_response(content)
            
            # If no context was provided, force low confidence
            if not context_parts:
                confidence = min(confidence, 20.0)
                if answer == content:
                    answer = "I couldn't find relevant information in your accessible projects."
            
            # Extract usage
            usage = response.usage
            
            logger.info(
                "answer_generated",
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                model=self.settings.openai_chat_model
            )
            
            return LLMResponse(
                answer=answer,
                confidence_score=confidence,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
                model=self.settings.openai_chat_model
            )
            
        except OpenAIError as e:
            logger.error("answer_generation_failed", error=str(e))
            raise LLMServiceError(f"Answer generation failed: {e}")
    
    def _parse_response(self, content: str) -> Tuple[float, str]:
        """
        Parse LLM response to extract confidence and answer.
        
        Returns:
            Tuple of (confidence_score, answer_text)
        """
        confidence = 50.0  # Default confidence
        answer = content
        
        # Try to extract confidence score
        if "CONFIDENCE:" in content.upper():
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if "CONFIDENCE:" in line.upper():
                    try:
                        # Extract number from line
                        score_str = ''.join(c for c in line if c.isdigit() or c == '.')
                        if score_str:
                            confidence = min(100.0, max(0.0, float(score_str)))
                    except ValueError:
                        pass
                    
                    # Answer is everything after confidence line
                    remaining_lines = lines[i+1:]
                    answer = '\n'.join(remaining_lines)
                    break
        
        # Clean up answer
        if "ANSWER:" in answer.upper():
            idx = answer.upper().find("ANSWER:")
            answer = answer[idx + 7:].strip()
        
        answer = answer.strip()
        
        # Ensure answer is not empty
        if not answer:
            answer = content
        
        return confidence, answer
    
    async def health_check(self) -> bool:
        """Check if LLM service is healthy."""
        try:
            # Quick test with minimal tokens
            await self.client.models.retrieve(self.settings.openai_chat_model)
            return True
        except Exception:
            return False


# Singleton instance
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """Get or create LLM service singleton."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
