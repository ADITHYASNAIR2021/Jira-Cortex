/**
 * Jira Cortex - Context Panel Component
 *
 * AI assistant panel in issue sidebar.
 * "Solve This" button for finding similar resolved issues.
 */

import React, { useState, useEffect } from 'react';
import ForgeReconciler, {
    Button,
    Text,
    Box,
    Stack,
    Inline,
    Badge,
    Link,
    SectionMessage,
    Spinner,
    xcss
} from '@forge/react';
import { invoke, view } from '@forge/bridge';

// Styles
const containerStyles = xcss({
    padding: 'space.200',
});

const resultCardStyles = xcss({
    padding: 'space.150',
    backgroundColor: 'color.background.neutral.subtle',
    borderRadius: 'border.radius.100',
    marginTop: 'space.100',
});

const confidenceStyles = (score) => xcss({
    color: score >= 70 ? 'color.text.success' : score >= 50 ? 'color.text.warning' : 'color.text.danger',
    fontWeight: 'bold',
});

const citationStyles = xcss({
    marginTop: 'space.100',
    padding: 'space.100',
    backgroundColor: 'color.background.neutral',
    borderRadius: 'border.radius.050',
});

export function ContextPanel() {
    const [loading, setLoading] = useState(false);
    const [result, setResult] = useState(null);
    const [error, setError] = useState(null);
    const [issueContext, setIssueContext] = useState(null);

    // Get current issue context on mount
    useEffect(() => {
        async function getIssueContext() {
            try {
                const context = await view.getContext();
                setIssueContext({
                    issueKey: context.extension.issue.key,
                    issueId: context.extension.issue.id,
                    issueSummary: context.extension.issue.summary,
                });
            } catch (err) {
                console.error('Failed to get issue context:', err);
            }
        }
        getIssueContext();
    }, []);

    /**
     * Handle "Solve This" button click
     */
    const handleSolveThis = async () => {
        if (!issueContext) {
            setError('Unable to read current issue context');
            return;
        }

        setLoading(true);
        setError(null);
        setResult(null);

        try {
            // Call resolver to query backend
            const response = await invoke('queryResolver', {
                query: `Find similar issues and solutions for: ${issueContext.issueSummary}`,
                context: {
                    currentIssueKey: issueContext.issueKey,
                    currentIssueSummary: issueContext.issueSummary,
                }
            });

            setResult(response);
        } catch (err) {
            console.error('Query failed:', err);
            setError(err.message || 'Failed to find solutions. Please try again.');
        } finally {
            setLoading(false);
        }
    };

    /**
     * Render confidence badge with color coding
     */
    const renderConfidenceBadge = (score) => {
        const appearance = score >= 70 ? 'success' : score >= 50 ? 'warning' : 'danger';
        return (
            <Badge appearance={appearance}>
                {Math.round(score)}% Confidence
            </Badge>
        );
    };

    /**
     * Render citations
     */
    const renderCitations = (citations) => {
        if (!citations || citations.length === 0) return null;

        return (
            <Box xcss={citationStyles}>
                <Text weight="medium" size="small">Sources:</Text>
                <Stack space="space.050">
                    {citations.map((citation, index) => (
                        <Inline key={index} space="space.100" alignBlock="center">
                            <Link href={citation.url} openNewTab>
                                {citation.issue_key}
                            </Link>
                            <Text size="small" color="color.text.subtlest">
                                ({Math.round(citation.relevance_score * 100)}% match)
                            </Text>
                        </Inline>
                    ))}
                </Stack>
            </Box>
        );
    };

    return (
        <Box xcss={containerStyles}>
            <Stack space="space.200">
                {/* Header */}
                <Inline spread="space-between" alignBlock="center">
                    <Text weight="bold" size="large">Cortex AI</Text>
                    {result && !loading && renderConfidenceBadge(result.confidence_score)}
                </Inline>

                {/* Solve This Button */}
                <Button
                    appearance="primary"
                    onClick={handleSolveThis}
                    isDisabled={loading || !issueContext}
                    shouldFitContainer
                >
                    {loading ? (
                        <Inline space="space.100" alignBlock="center">
                            <Spinner size="small" />
                            <Text>Analyzing...</Text>
                        </Inline>
                    ) : (
                        '🔍 Solve This'
                    )}
                </Button>

                {/* Error Message */}
                {error && (
                    <SectionMessage appearance="error">
                        <Text>{error}</Text>
                    </SectionMessage>
                )}

                {/* Result */}
                {result && !loading && (
                    <Box xcss={resultCardStyles}>
                        <Stack space="space.150">
                            {/* Answer */}
                            <Text>{result.answer}</Text>

                            {/* Citations */}
                            {renderCitations(result.citations)}

                            {/* Metadata */}
                            <Inline space="space.200">
                                <Text size="small" color="color.text.subtlest">
                                    {result.processing_time_ms}ms
                                </Text>
                                {result.cached && (
                                    <Badge appearance="default">Cached</Badge>
                                )}
                            </Inline>
                        </Stack>
                    </Box>
                )}

                {/* Empty State */}
                {!result && !loading && !error && (
                    <SectionMessage appearance="information">
                        <Text>
                            Click "Solve This" to find similar resolved issues and solutions.
                        </Text>
                    </SectionMessage>
                )}
            </Stack>
        </Box>
    );
}

// Export for Forge
export default ContextPanel;
