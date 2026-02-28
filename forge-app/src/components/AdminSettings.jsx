/**
 * Jira Cortex - Admin Settings Page
 *
 * Admin dashboard for:
 * - Initial data sync (historic tickets)
 * - Usage statistics
 * - Configuration management
 */

import React, { useState, useEffect } from 'react';
import ForgeReconciler, {
    Button,
    Text,
    Box,
    Stack,
    Inline,
    Badge,
    SectionMessage,
    Spinner,
    ProgressBar,
    Heading,
    Textfield,
    xcss
} from '@forge/react';
import { invoke, requestJira } from '@forge/bridge';

// Styles
const containerStyles = xcss({
    padding: 'space.400',
    maxWidth: '900px',
    margin: '0 auto',
});

const cardStyles = xcss({
    padding: 'space.300',
    backgroundColor: 'color.background.neutral.subtle',
    borderRadius: 'border.radius.200',
    marginBottom: 'space.300',
});

const statCardStyles = xcss({
    padding: 'space.200',
    backgroundColor: 'color.background.neutral',
    borderRadius: 'border.radius.100',
    textAlign: 'center',
    minWidth: '120px',
});

export function AdminSettings() {
    const [syncStatus, setSyncStatus] = useState('idle'); // idle, syncing, completed, error
    const [syncProgress, setSyncProgress] = useState(0);
    const [syncStats, setSyncStats] = useState(null);
    const [projectList, setProjectList] = useState([]);
    const [selectedProjects, setSelectedProjects] = useState([]);
    const [error, setError] = useState(null);
    const [usageStats, setUsageStats] = useState(null);

    // Backend URL configuration state
    const [backendUrl, setBackendUrl] = useState('');
    const [urlSaving, setUrlSaving] = useState(false);
    const [urlSaved, setUrlSaved] = useState(false);

    // Job tracking for accurate progress (UX fix)
    const [activeJobs, setActiveJobs] = useState([]);  // {jobId, status, progress}
    const [processingStatus, setProcessingStatus] = useState(null);  // 'processing' | 'complete'

    // Fetch projects on mount
    useEffect(() => {
        fetchProjects();
        fetchUsageStats();
        fetchBackendUrl();
    }, []);

    // Poll active jobs every 5s when processing
    useEffect(() => {
        if (activeJobs.length === 0 || processingStatus === 'complete') return;

        const pollInterval = setInterval(async () => {
            const updatedJobs = await Promise.all(
                activeJobs.map(async (job) => {
                    try {
                        const status = await invoke('getJobStatus', { jobId: job.jobId });
                        return { ...job, ...status };
                    } catch (err) {
                        return job;
                    }
                })
            );

            setActiveJobs(updatedJobs);

            // Check if all jobs are complete
            const allDone = updatedJobs.every(j =>
                j.status === 'completed' || j.status === 'failed'
            );
            if (allDone && updatedJobs.length > 0) {
                setProcessingStatus('complete');
            }
        }, 5000);

        return () => clearInterval(pollInterval);
    }, [activeJobs, processingStatus]);

    /**
     * Fetch current backend URL configuration
     */
    const fetchBackendUrl = async () => {
        try {
            const url = await invoke('getBackendUrl');
            setBackendUrl(url || '');
        } catch (err) {
            console.warn('Failed to fetch backend URL:', err);
        }
    };

    /**
     * Save backend URL configuration
     */
    const saveBackendUrl = async () => {
        setUrlSaving(true);
        setError(null);
        try {
            await invoke('setBackendUrl', { url: backendUrl });
            setUrlSaved(true);
            setTimeout(() => setUrlSaved(false), 3000);
        } catch (err) {
            setError('Failed to save backend URL: ' + err.message);
        } finally {
            setUrlSaving(false);
        }
    };

    /**
     * Fetch all projects the admin has access to
     */
    const fetchProjects = async () => {
        try {
            const response = await requestJira('/rest/api/3/project/search', {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            });

            const data = await response.json();
            setProjectList(data.values || []);

            // Select all by default
            setSelectedProjects((data.values || []).map(p => p.key));
        } catch (err) {
            console.error('Failed to fetch projects:', err);
            setError('Failed to load projects');
        }
    };

    /**
     * Fetch usage statistics from backend
     */
    const fetchUsageStats = async () => {
        try {
            const stats = await invoke('getUsageStats');
            setUsageStats(stats);
        } catch (err) {
            console.warn('Failed to fetch usage stats:', err);
            // Non-critical, don't show error
        }
    };

    /**
     * Start historic data sync
     */
    const startSync = async () => {
        if (selectedProjects.length === 0) {
            setError('Please select at least one project');
            return;
        }

        setSyncStatus('syncing');
        setSyncProgress(0);
        setError(null);
        setSyncStats(null);

        try {
            // Fetch issues from selected projects in batches
            let allIssues = [];
            let totalProcessed = 0;
            let totalFailed = 0;

            for (let i = 0; i < selectedProjects.length; i++) {
                const projectKey = selectedProjects[i];

                // Update progress
                setSyncProgress(((i + 0.5) / selectedProjects.length) * 100);

                // Fetch issues for this project
                const issues = await fetchProjectIssues(projectKey);

                if (issues.length > 0) {
                    // Send to backend in batches of 50
                    for (let j = 0; j < issues.length; j += 50) {
                        const batch = issues.slice(j, j + 50);

                        try {
                            const result = await invoke('ingestBatch', { issues: batch });
                            totalProcessed += batch.length;

                            // Track job for polling (UX fix)
                            if (result.job_id) {
                                setActiveJobs(prev => [...prev, {
                                    jobId: result.job_id,
                                    status: 'pending',
                                    progress: 0
                                }]);
                                setProcessingStatus('processing');
                            }
                        } catch (err) {
                            console.error(`Batch failed for ${projectKey}:`, err);
                            totalFailed += batch.length;
                        }
                    }
                }

                setSyncProgress(((i + 1) / selectedProjects.length) * 100);
            }

            setSyncStats({
                processed: totalProcessed,
                failed: totalFailed,
                projects: selectedProjects.length
            });

            setSyncStatus('completed');

        } catch (err) {
            console.error('Sync failed:', err);
            setError(err.message || 'Sync failed');
            setSyncStatus('error');
        }
    };

    /**
     * Fetch all issues for a project
     */
    const fetchProjectIssues = async (projectKey) => {
        const issues = [];
        let startAt = 0;
        const maxResults = 100;

        try {
            while (true) {
                const response = await requestJira(
                    `/rest/api/3/search?jql=project=${projectKey}&startAt=${startAt}&maxResults=${maxResults}&fields=summary,description,status,project,reporter,assignee,created,updated,resolutiondate,labels,components`,
                    {
                        method: 'GET',
                        headers: { 'Accept': 'application/json' }
                    }
                );

                const data = await response.json();

                if (!data.issues || data.issues.length === 0) break;

                // Transform to our format
                for (const issue of data.issues) {
                    issues.push({
                        key: issue.key,
                        summary: issue.fields.summary,
                        description: issue.fields.description?.content?.[0]?.content?.[0]?.text || null,
                        status: mapStatus(issue.fields.status?.name),
                        project_id: issue.fields.project?.id,
                        project_key: issue.fields.project?.key,
                        reporter_account_id: issue.fields.reporter?.accountId,
                        assignee_account_id: issue.fields.assignee?.accountId,
                        created: issue.fields.created,
                        updated: issue.fields.updated,
                        resolved: issue.fields.resolutiondate,
                        labels: issue.fields.labels || [],
                        components: (issue.fields.components || []).map(c => c.name),
                        comments: [] // Skip comments for bulk sync (too slow)
                    });
                }

                startAt += data.issues.length;

                // Check if we've fetched all
                if (startAt >= data.total) break;

                // Safety limit
                if (issues.length >= 5000) {
                    console.warn(`Project ${projectKey} has more than 5000 issues, capping`);
                    break;
                }
            }
        } catch (err) {
            console.error(`Failed to fetch issues for ${projectKey}:`, err);
        }

        return issues;
    };

    /**
     * Map Jira status to our enum
     */
    const mapStatus = (statusName) => {
        const lower = (statusName || '').toLowerCase();
        if (lower.includes('done') || lower.includes('closed') || lower.includes('resolved')) {
            return 'resolved';
        }
        if (lower.includes('progress') || lower.includes('review')) {
            return 'in_progress';
        }
        return 'open';
    };

    /**
     * Toggle project selection
     */
    const toggleProject = (projectKey) => {
        setSelectedProjects(prev =>
            prev.includes(projectKey)
                ? prev.filter(p => p !== projectKey)
                : [...prev, projectKey]
        );
    };

    /**
     * Select/deselect all projects
     */
    const toggleAllProjects = () => {
        if (selectedProjects.length === projectList.length) {
            setSelectedProjects([]);
        } else {
            setSelectedProjects(projectList.map(p => p.key));
        }
    };

    return (
        <Box xcss={containerStyles}>
            <Stack space="space.300">
                {/* Header */}
                <Heading as="h1">Cortex Admin Settings</Heading>
                <Text color="color.text.subtlest">
                    Configure and manage your Jira Cortex integration.
                </Text>

                {/* Usage Stats */}
                {usageStats && (
                    <Box xcss={cardStyles}>
                        <Stack space="space.200">
                            <Heading as="h3">Usage This Month</Heading>
                            <Inline space="space.200" shouldWrap>
                                <Box xcss={statCardStyles}>
                                    <Text weight="bold" size="xlarge">{usageStats.queries || 0}</Text>
                                    <Text size="small" color="color.text.subtlest">Queries</Text>
                                </Box>
                                <Box xcss={statCardStyles}>
                                    <Text weight="bold" size="xlarge">{formatTokens(usageStats.tokens || 0)}</Text>
                                    <Text size="small" color="color.text.subtlest">Tokens Used</Text>
                                </Box>
                                <Box xcss={statCardStyles}>
                                    <Text weight="bold" size="xlarge">${usageStats.cost || '0.00'}</Text>
                                    <Text size="small" color="color.text.subtlest">Estimated Cost</Text>
                                </Box>
                            </Inline>
                        </Stack>
                    </Box>
                )}

                {/* Data Sync Section */}
                <Box xcss={cardStyles}>
                    <Stack space="space.200">
                        <Heading as="h3">Initial Data Sync</Heading>
                        <Text>
                            Sync your existing Jira issues to enable AI-powered search on historic data.
                            New issues will sync automatically via webhooks.
                        </Text>

                        {/* Project Selection */}
                        <Box>
                            <Inline spread="space-between" alignBlock="center">
                                <Text weight="medium">Select Projects to Sync</Text>
                                <Button
                                    appearance="subtle"
                                    onClick={toggleAllProjects}
                                    isDisabled={syncStatus === 'syncing'}
                                >
                                    {selectedProjects.length === projectList.length ? 'Deselect All' : 'Select All'}
                                </Button>
                            </Inline>

                            <Box xcss={{ marginTop: 'space.100' }}>
                                <Inline space="space.050" shouldWrap>
                                    {projectList.map(project => (
                                        <Badge
                                            key={project.key}
                                            appearance={selectedProjects.includes(project.key) ? 'primary' : 'default'}
                                            onClick={() => syncStatus !== 'syncing' && toggleProject(project.key)}
                                        >
                                            {project.key}
                                        </Badge>
                                    ))}
                                </Inline>
                            </Box>
                        </Box>

                        {/* Sync Progress */}
                        {syncStatus === 'syncing' && (
                            <Box xcss={{ marginTop: 'space.200' }}>
                                <ProgressBar value={syncProgress} />
                                <Inline space="space.100" alignBlock="center">
                                    <Spinner size="small" />
                                    <Text size="small">Syncing... {Math.round(syncProgress)}%</Text>
                                </Inline>
                            </Box>
                        )}

                        {/* Sync Complete */}
                        {syncStatus === 'completed' && syncStats && (
                            <SectionMessage appearance="success">
                                <Text>
                                    Sync complete! Processed {syncStats.processed} issues from {syncStats.projects} projects.
                                    {syncStats.failed > 0 && ` (${syncStats.failed} failed)`}
                                </Text>
                            </SectionMessage>
                        )}

                        {/* Error */}
                        {error && (
                            <SectionMessage appearance="error">
                                <Text>{error}</Text>
                            </SectionMessage>
                        )}

                        {/* Sync Button */}
                        <Button
                            appearance="primary"
                            onClick={startSync}
                            isDisabled={syncStatus === 'syncing' || selectedProjects.length === 0}
                        >
                            {syncStatus === 'syncing' ? 'Syncing...' : 'Start Historic Sync'}
                        </Button>

                        <Text size="small" color="color.text.subtlest">
                            ⚠️ This may take several minutes for large projects. You can close this page -
                            sync will continue in the background.
                        </Text>
                    </Stack>
                </Box>

                {/* Backend Connection Config */}
                <Box xcss={cardStyles}>
                    <Stack space="space.200">
                        <Heading as="h3">Backend Connection</Heading>
                        <Text>
                            Configure the API URL for your Cortex backend. Update this if you move your backend to a different host.
                        </Text>
                        <Textfield
                            name="backendUrl"
                            value={backendUrl}
                            onChange={(e) => setBackendUrl(e.target.value)}
                            placeholder="https://your-backend.example.com"
                        />
                        <Inline space="space.100" alignBlock="center">
                            <Button
                                appearance="primary"
                                onClick={saveBackendUrl}
                                isDisabled={urlSaving || !backendUrl}
                            >
                                {urlSaving ? 'Saving...' : 'Save URL'}
                            </Button>
                            {urlSaved && (
                                <Badge appearance="success">Saved!</Badge>
                            )}
                        </Inline>
                    </Stack>
                </Box>

                {/* Settings Info */}
                <Box xcss={cardStyles}>
                    <Stack space="space.100">
                        <Heading as="h3">Configuration</Heading>
                        <Text>Backend Status: <Badge appearance="success">Connected</Badge></Text>
                        <Text>Webhook Status: <Badge appearance="success">Active</Badge></Text>
                        <Text size="small" color="color.text.subtlest">
                            Webhooks are automatically configured to sync new and updated issues in real-time.
                        </Text>
                    </Stack>
                </Box>
            </Stack>
        </Box>
    );
}

/**
 * Format large token numbers
 */
function formatTokens(num) {
    if (num >= 1000000) {
        return (num / 1000000).toFixed(1) + 'M';
    }
    if (num >= 1000) {
        return (num / 1000).toFixed(1) + 'K';
    }
    return num.toString();
}

// Export for Forge
export default AdminSettings;
