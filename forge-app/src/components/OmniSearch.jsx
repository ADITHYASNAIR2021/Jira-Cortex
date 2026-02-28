/**
 * Jira Cortex - Omni Search Component
 *
 * Global chat interface for querying the knowledge base.
 * ChatGPT-style conversational UI.
 */

import React, { useState, useRef, useEffect } from 'react';
import ForgeReconciler, {
    Button,
    Text,
    Textfield,
    Box,
    Stack,
    Inline,
    Badge,
    Link,
    SectionMessage,
    Spinner,
    xcss
} from '@forge/react';
import { invoke } from '@forge/bridge';

// Styles
const pageContainerStyles = xcss({
    padding: 'space.400',
    maxWidth: '800px',
    margin: '0 auto',
});

const headerStyles = xcss({
    marginBottom: 'space.300',
});

const chatContainerStyles = xcss({
    minHeight: '400px',
    maxHeight: '600px',
    overflowY: 'auto',
    padding: 'space.200',
    backgroundColor: 'color.background.neutral.subtle',
    borderRadius: 'border.radius.200',
    marginBottom: 'space.200',
});

const messageStyles = (isUser) => xcss({
    padding: 'space.150',
    backgroundColor: isUser ? 'color.background.brand.bold' : 'color.background.neutral',
    color: isUser ? 'color.text.inverse' : 'color.text',
    borderRadius: 'border.radius.100',
    marginBottom: 'space.100',
    maxWidth: '80%',
    alignSelf: isUser ? 'flex-end' : 'flex-start',
});

const inputContainerStyles = xcss({
    marginTop: 'space.200',
});

const citationStyles = xcss({
    marginTop: 'space.100',
    padding: 'space.100',
    backgroundColor: 'color.background.neutral',
    borderRadius: 'border.radius.050',
    fontSize: '12px',
});

export function OmniSearch() {
    const [messages, setMessages] = useState([]);
    const [inputValue, setInputValue] = useState('');
    const [loading, setLoading] = useState(false);
    const chatEndRef = useRef(null);

    // Auto-scroll to bottom of chat
    useEffect(() => {
        chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    /**
     * Handle sending a message
     */
    const handleSend = async () => {
        if (!inputValue.trim() || loading) return;

        const userMessage = {
            id: Date.now(),
            type: 'user',
            content: inputValue.trim(),
            timestamp: new Date().toISOString(),
        };

        setMessages(prev => [...prev, userMessage]);
        setInputValue('');
        setLoading(true);

        try {
            // Call resolver to query backend
            const response = await invoke('queryResolver', {
                query: userMessage.content,
                context: null
            });

            const assistantMessage = {
                id: Date.now() + 1,
                type: 'assistant',
                content: response.answer,
                confidence: response.confidence_score,
                citations: response.citations,
                processingTime: response.processing_time_ms,
                cached: response.cached,
                timestamp: new Date().toISOString(),
            };

            setMessages(prev => [...prev, assistantMessage]);

        } catch (err) {
            console.error('Query failed:', err);

            const errorMessage = {
                id: Date.now() + 1,
                type: 'error',
                content: err.message || 'Failed to get response. Please try again.',
                timestamp: new Date().toISOString(),
            };

            setMessages(prev => [...prev, errorMessage]);
        } finally {
            setLoading(false);
        }
    };

    /**
     * Handle Enter key press
     */
    const handleKeyPress = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    /**
     * Render a single message
     */
    const renderMessage = (message) => {
        if (message.type === 'user') {
            return (
                <Box key={message.id} xcss={messageStyles(true)}>
                    <Text color="color.text.inverse">{message.content}</Text>
                </Box>
            );
        }

        if (message.type === 'error') {
            return (
                <Box key={message.id}>
                    <SectionMessage appearance="error">
                        <Text>{message.content}</Text>
                    </SectionMessage>
                </Box>
            );
        }

        // Assistant message
        return (
            <Box key={message.id} xcss={messageStyles(false)}>
                <Stack space="space.100">
                    {/* Confidence Badge */}
                    <Inline space="space.100">
                        <Badge
                            appearance={
                                message.confidence >= 70 ? 'success' :
                                    message.confidence >= 50 ? 'warning' : 'danger'
                            }
                        >
                            {Math.round(message.confidence)}% Confidence
                        </Badge>
                        {message.cached && <Badge appearance="default">Cached</Badge>}
                    </Inline>

                    {/* Answer */}
                    <Text>{message.content}</Text>

                    {/* Citations */}
                    {message.citations && message.citations.length > 0 && (
                        <Box xcss={citationStyles}>
                            <Text weight="medium" size="small">Sources:</Text>
                            <Stack space="space.050">
                                {message.citations.map((citation, index) => (
                                    <Inline key={index} space="space.100">
                                        <Link href={citation.url} openNewTab>
                                            {citation.issue_key}: {citation.title.substring(0, 50)}
                                            {citation.title.length > 50 ? '...' : ''}
                                        </Link>
                                    </Inline>
                                ))}
                            </Stack>
                        </Box>
                    )}

                    {/* Processing Time */}
                    <Text size="small" color="color.text.subtlest">
                        Processed in {message.processingTime}ms
                    </Text>
                </Stack>
            </Box>
        );
    };

    return (
        <Box xcss={pageContainerStyles}>
            {/* Header */}
            <Box xcss={headerStyles}>
                <Stack space="space.100">
                    <Inline space="space.100" alignBlock="center">
                        <Text weight="bold" size="xlarge">🧠 Cortex Search</Text>
                    </Inline>
                    <Text color="color.text.subtlest">
                        Ask questions about your Jira issues and get AI-powered answers with citations.
                    </Text>
                </Stack>
            </Box>

            {/* Chat Container */}
            <Box xcss={chatContainerStyles}>
                <Stack space="space.100">
                    {messages.length === 0 ? (
                        <SectionMessage appearance="information">
                            <Stack space="space.100">
                                <Text weight="medium">Welcome to Cortex Search!</Text>
                                <Text>Try asking questions like:</Text>
                                <Text size="small" color="color.text.subtlest">
                                    • "Has this login bug happened before?"
                                </Text>
                                <Text size="small" color="color.text.subtlest">
                                    • "What is the status of the Mobile App release?"
                                </Text>
                                <Text size="small" color="color.text.subtlest">
                                    • "How did we fix the Redis timeout issue?"
                                </Text>
                            </Stack>
                        </SectionMessage>
                    ) : (
                        messages.map(renderMessage)
                    )}

                    {/* Loading indicator */}
                    {loading && (
                        <Inline space="space.100" alignBlock="center">
                            <Spinner size="small" />
                            <Text color="color.text.subtlest">Searching...</Text>
                        </Inline>
                    )}

                    <div ref={chatEndRef} />
                </Stack>
            </Box>

            {/* Input Area */}
            <Box xcss={inputContainerStyles}>
                <Inline space="space.100">
                    <Box xcss={{ flexGrow: 1 }}>
                        <Textfield
                            value={inputValue}
                            onChange={(e) => setInputValue(e.target.value)}
                            onKeyPress={handleKeyPress}
                            placeholder="Ask a question about your projects..."
                            isDisabled={loading}
                        />
                    </Box>
                    <Button
                        appearance="primary"
                        onClick={handleSend}
                        isDisabled={!inputValue.trim() || loading}
                    >
                        Send
                    </Button>
                </Inline>
            </Box>
        </Box>
    );
}

// Export for Forge
export default OmniSearch;
