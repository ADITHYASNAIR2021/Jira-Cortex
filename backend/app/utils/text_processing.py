"""
Jira Cortex - Text Processing Utilities

Secure text processing with robust secret detection.
Uses detect-secrets library instead of regex for compliance.
"""

import re
import hashlib
from typing import List, Tuple, Optional
from dataclasses import dataclass
import structlog
from bs4 import BeautifulSoup
import tiktoken

# Use detect-secrets for robust secret detection
from detect_secrets import SecretsCollection
from detect_secrets.settings import transient_settings

from app.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass
class TextChunk:
    """A chunk of processed text with metadata."""
    content: str
    chunk_index: int
    total_chunks: int
    token_count: int
    content_hash: str


class SecretDetector:
    """
    Robust secret detection using detect-secrets library.
    
    Detects:
    - API keys (AWS, Google, GitHub, etc.)
    - Private keys
    - Passwords and tokens
    - Database connection strings
    - And many more secret patterns
    """
    
    # Additional patterns for secrets that detect-secrets might miss or not expose the secret_value for
    ADDITIONAL_PATTERNS = [
        # Jira/Atlassian API tokens
        (r'ATATT3x[A-Za-z0-9-_]{50,}', '[ATLASSIAN_TOKEN_REDACTED]'),
        # GitHub tokens
        (r'ghp_[a-zA-Z0-9]{36}', '[GITHUB_TOKEN_REDACTED]'),
        # AWS Access Key IDs
        (r'(?<![A-Z0-9])[A-Z0-9]{20}(?![A-Z0-9])', '[AWS_KEY_REDACTED]'),
        # Generic high-entropy strings that look like secrets
        (r'(?:api[_-]?key|apikey|secret|password|token|auth)["\']?\s*[:=]\s*["\']?([A-Za-z0-9+/_]{20,})', r'\g<0>_MASKED'),
        # JWT tokens in text
        (r'eyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+', '[JWT_REDACTED]'),
    ]
    
    def __init__(self):
        self.settings = get_settings()
        
    def detect_and_mask(self, text: str) -> Tuple[str, int]:
        """
        Detect secrets in text and mask them.
        
        Args:
            text: Input text to scan
            
        Returns:
            Tuple of (sanitized_text, secrets_found_count)
        """
        secrets_found = 0
        sanitized = text
        
        try:
            # Use detect-secrets inline scanning
            with transient_settings({
                'plugins_used': [
                    {'name': 'AWSKeyDetector'},
                    {'name': 'ArtifactoryDetector'},
                    {'name': 'AzureStorageKeyDetector'},
                    {'name': 'BasicAuthDetector'},
                    {'name': 'CloudantDetector'},
                    {'name': 'DiscordBotTokenDetector'},
                    {'name': 'GitHubTokenDetector'},
                    {'name': 'IbmCloudIamDetector'},
                    {'name': 'IbmCosHmacDetector'},
                    {'name': 'JwtTokenDetector'},
                    {'name': 'MailchimpDetector'},
                    {'name': 'NpmDetector'},
                    {'name': 'PrivateKeyDetector'},
                    {'name': 'SendGridDetector'},
                    {'name': 'SlackDetector'},
                    {'name': 'SoftlayerDetector'},
                    {'name': 'SquareOAuthDetector'},
                    {'name': 'StripeDetector'},
                    {'name': 'TwilioKeyDetector'},
                ]
            }):
                secrets = SecretsCollection()
                secrets.scan_string(text)
                
                # Build list of secret positions and mask them
                for file_path, secrets_list in secrets.data.items():
                    for secret in secrets_list:
                        secrets_found += 1
                        # Mask the secret value
                        secret_type = secret.type.replace('Detector', '').upper()
                        mask = f'[{secret_type}_REDACTED]'
                        
                        # Replace the secret in text
                        secret_value = secret.secret_value
                        if secret_value:
                            sanitized = sanitized.replace(secret_value, mask)
            
            # Apply additional patterns
            for pattern, replacement in self.ADDITIONAL_PATTERNS:
                matches = re.finditer(pattern, sanitized, flags=re.IGNORECASE)
                match_count = sum(1 for _ in matches)
                if match_count > 0:
                    secrets_found += match_count
                    if "MASKED" in replacement:
                        # For the generic pattern, just replace with the redacted string
                        sanitized = re.sub(pattern, '[SECRET_REDACTED]', sanitized, flags=re.IGNORECASE)
                    else:
                        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
            
            if secrets_found > 0:
                logger.warning("secrets_detected_and_masked", count=secrets_found)
                
        except Exception as e:
            logger.error("secret_detection_error", error=str(e))
            # On error, still apply regex patterns and count matches
            for pattern, replacement in self.ADDITIONAL_PATTERNS:
                matches = re.findall(pattern, sanitized, flags=re.IGNORECASE)
                if matches:
                    secrets_found += len(matches)
                repl = "[SECRET_REDACTED]" if "MASKED" in replacement else replacement
                sanitized = re.sub(pattern, repl, sanitized, flags=re.IGNORECASE)
        
        return sanitized, secrets_found


class HTMLCleaner:
    """Clean and extract text from HTML content."""
    
    # Tags to remove completely (including contents)
    REMOVE_TAGS = {'script', 'style', 'meta', 'link', 'noscript'}
    
    # Tags to preserve as-is for code
    CODE_TAGS = {'code', 'pre'}
    
    def clean(self, html: str) -> str:
        """
        Extract clean text from HTML while preserving code blocks.
        
        Args:
            html: Raw HTML content
            
        Returns:
            Clean text content
        """
        if not html:
            return ""
        
        try:
            soup = BeautifulSoup(html, 'lxml')
            
            # Remove unwanted tags
            for tag in soup.find_all(self.REMOVE_TAGS):
                tag.decompose()
            
            # Preserve code blocks with markers
            for code_tag in soup.find_all(self.CODE_TAGS):
                code_text = code_tag.get_text()
                code_tag.replace_with(f"\n```\n{code_text}\n```\n")
            
            # Get text with proper spacing
            text = soup.get_text(separator=' ', strip=True)
            
            # Clean up excessive whitespace
            text = re.sub(r'\s+', ' ', text)
            text = re.sub(r'\n\s*\n+', '\n\n', text)
            
            return text.strip()
            
        except Exception as e:
            logger.error("html_cleaning_error", error=str(e))
            # Fallback: basic tag stripping
            return re.sub(r'<[^>]+>', ' ', html).strip()


class TextChunker:
    """
    Split text into chunks for embedding.
    
    Uses tiktoken for accurate token counting.
    Preserves sentence boundaries where possible.
    """
    
    def __init__(self, chunk_size: int = 500, overlap: int = 50):
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.settings = get_settings()
        
        # Use cl100k_base encoding (same as text-embedding-3-small)
        self.tokenizer = tiktoken.get_encoding("cl100k_base")
    
    def count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        return len(self.tokenizer.encode(text))
    
    def chunk(self, text: str, doc_id: str = "") -> List[TextChunk]:
        """
        Split text into overlapping chunks.
        
        Args:
            text: Text to chunk
            doc_id: Optional document ID for hashing
            
        Returns:
            List of TextChunk objects
        """
        if not text:
            return []
        
        # Get tokens
        tokens = self.tokenizer.encode(text)
        total_tokens = len(tokens)
        
        if total_tokens <= self.chunk_size:
            # Text fits in single chunk
            return [TextChunk(
                content=text,
                chunk_index=0,
                total_chunks=1,
                token_count=total_tokens,
                content_hash=self._hash_content(text, doc_id, 0)
            )]
        
        chunks = []
        start = 0
        chunk_index = 0
        
        while start < total_tokens:
            # Calculate end position
            end = min(start + self.chunk_size, total_tokens)
            
            # Decode chunk tokens back to text
            chunk_tokens = tokens[start:end]
            chunk_text = self.tokenizer.decode(chunk_tokens)
            
            # Try to end at sentence boundary
            if end < total_tokens:
                chunk_text = self._adjust_to_sentence_boundary(chunk_text)
            
            chunks.append(TextChunk(
                content=chunk_text,
                chunk_index=chunk_index,
                total_chunks=0,  # Will be updated
                token_count=len(self.tokenizer.encode(chunk_text)),
                content_hash=self._hash_content(chunk_text, doc_id, chunk_index)
            ))
            
            # Move start with overlap
            start = end - self.overlap
            chunk_index += 1
        
        # Update total_chunks
        total_chunks = len(chunks)
        for chunk in chunks:
            chunk.total_chunks = total_chunks
        
        return chunks
    
    def _adjust_to_sentence_boundary(self, text: str) -> str:
        """Try to cut at sentence boundary."""
        # Find last sentence-ending punctuation
        for i in range(len(text) - 1, max(0, len(text) - 200), -1):
            if text[i] in '.!?' and (i + 1 >= len(text) or text[i + 1].isspace()):
                return text[:i + 1]
        
        # No sentence boundary found, try word boundary
        last_space = text.rfind(' ', max(0, len(text) - 100))
        if last_space > len(text) * 0.7:
            return text[:last_space]
        
        return text
    
    def _hash_content(self, content: str, doc_id: str, chunk_index: int) -> str:
        """Generate unique hash for chunk deduplication."""
        hash_input = f"{doc_id}:{chunk_index}:{content}"
        return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


class TextProcessor:
    """
    Main text processing pipeline.
    
    Combines HTML cleaning, secret masking, and chunking.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.html_cleaner = HTMLCleaner()
        self.secret_detector = SecretDetector()
        self.chunker = TextChunker(
            chunk_size=self.settings.chunk_size_tokens
        )
    
    def process(
        self, 
        text: str, 
        doc_id: str = "",
        is_html: bool = True
    ) -> Tuple[List[TextChunk], int]:
        """
        Process text through full pipeline.
        
        Args:
            text: Raw input text (possibly HTML)
            doc_id: Document identifier
            is_html: Whether input is HTML
            
        Returns:
            Tuple of (chunks, secrets_masked_count)
        """
        if not text:
            return [], 0
        
        # Step 1: Clean HTML if needed
        if is_html:
            text = self.html_cleaner.clean(text)
        
        # Step 2: Detect and mask secrets
        text, secrets_count = self.secret_detector.detect_and_mask(text)
        
        # Step 3: Chunk the text
        chunks = self.chunker.chunk(text, doc_id)
        
        logger.info(
            "text_processed",
            doc_id=doc_id,
            chunk_count=len(chunks),
            secrets_masked=secrets_count
        )
        
        return chunks, secrets_count
    
    def format_issue_for_embedding(
        self, 
        key: str,
        summary: str,
        description: Optional[str],
        status: str,
        labels: List[str] = None,
        comments: List[str] = None
    ) -> str:
        """
        Format a Jira issue into text for embedding.
        """
        parts = [
            f"Issue: {key}",
            f"Title: {summary}",
            f"Status: {status}"
        ]
        
        if labels:
            parts.append(f"Labels: {', '.join(labels)}")
        
        if description:
            # Clean HTML from description
            clean_desc = self.html_cleaner.clean(description)
            parts.append(f"Description: {clean_desc}")
        
        if comments:
            # Include last few comments
            recent_comments = comments[-5:]  # Last 5 comments
            for i, comment in enumerate(recent_comments, 1):
                clean_comment = self.html_cleaner.clean(comment)
                parts.append(f"Comment {i}: {clean_comment}")
        
        return "\n\n".join(parts)

    def format_confluence_page_for_embedding(
        self,
        page_id: str,
        title: str,
        body: Optional[str],
        space_key: str,
        labels: List[str] = None
    ) -> str:
        """
        Format a Confluence page into text for embedding.
        """
        parts = [
            "Type: Confluence Page",
            f"Space: {space_key}",
            f"Page ID: {page_id}",
            f"Title: {title}",
        ]
        
        if labels:
            parts.append(f"Labels: {', '.join(labels)}")
            
        if body:
            clean_body = self.html_cleaner.clean(body)
            parts.append(f"Content: {clean_body}")
            
        return "\n\n".join(parts)


# Singleton instance
_processor: Optional[TextProcessor] = None


def get_text_processor() -> TextProcessor:
    """Get or create text processor singleton."""
    global _processor
    if _processor is None:
        _processor = TextProcessor()
    return _processor
