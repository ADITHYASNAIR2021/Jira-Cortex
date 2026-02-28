/**
 * Jira Cortex - Issue Update Trigger
 *
 * Webhook handler for real-time issue sync.
 * Solves Trap 2: Stale Data Problem
 * 
 * FIXED: Now throws errors for transient failures to enable Jira retry.
 */

import api, { route } from '@forge/api';
import { ingestSingle } from '../api/backend';

// Error class for transient failures that should be retried
class TransientError extends Error {
    constructor(message) {
        super(message);
        this.name = 'TransientError';
        this.retryable = true;
    }
}

/**
 * Extract issue data from Jira API response
 */
async function getIssueData(cloudId, issueId) {
    const response = await api.asApp().requestJira(route`/rest/api/3/issue/${issueId}`, {
        headers: {
            'Accept': 'application/json'
        }
    });

    if (!response.ok) {
        const status = response.status;
        // 5xx errors are transient - should retry
        if (status >= 500) {
            throw new TransientError(`Jira API returned ${status}`);
        }
        throw new Error(`Failed to fetch issue: ${status}`);
    }

    const issue = await response.json();

    return {
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
        comments: await getComments(cloudId, issueId)
    };
}

/**
 * Get issue comments
 */
async function getComments(cloudId, issueId) {
    try {
        const response = await api.asApp().requestJira(
            route`/rest/api/3/issue/${issueId}/comment`,
            {
                headers: {
                    'Accept': 'application/json'
                }
            }
        );

        if (!response.ok) {
            return [];
        }

        const data = await response.json();

        // Extract text from comments (last 5)
        return (data.comments || [])
            .slice(-5)
            .map(c => c.body?.content?.[0]?.content?.[0]?.text || '')
            .filter(Boolean);

    } catch (err) {
        console.warn('Failed to fetch comments:', err.message);
        return [];
    }
}

/**
 * Map Jira status to our schema
 */
function mapStatus(statusName) {
    const lower = (statusName || '').toLowerCase();

    if (lower.includes('done') || lower.includes('closed') || lower.includes('resolved')) {
        return 'resolved';
    }
    if (lower.includes('progress') || lower.includes('review')) {
        return 'in_progress';
    }
    return 'open';
}

/**
 * Handle issue created/updated events
 * 
 * IMPORTANT: For transient errors, we throw instead of returning false.
 * This signals to Jira that the webhook should be retried.
 */
export async function onIssueChange(event, context) {
    const { issue, changelog } = event;
    const cloudId = context.cloudId;

    console.log(`Issue change event: ${issue.key}`, {
        eventType: event.eventType,
        cloudId
    });

    try {
        // Get full issue data
        const issueData = await getIssueData(cloudId, issue.id);

        // Determine event type
        const eventType = event.eventType?.includes('created') ? 'created' : 'updated';

        // Send to backend for ingestion
        const result = await ingestSingle(issueData, cloudId, eventType);

        console.log(`Issue synced: ${issue.key}`, result);

        return {
            success: true,
            issueKey: issue.key,
            status: result.status
        };

    } catch (err) {
        console.error(`Failed to sync issue ${issue.key}:`, err.message);

        // FIXED: For transient errors, throw to trigger Jira retry
        if (err.retryable || err.message.includes('503') || err.message.includes('timeout')) {
            throw new Error(`Transient failure syncing ${issue.key}: ${err.message}`);
        }

        // For permanent errors (400, 401, etc.), return false but don't retry
        return {
            success: false,
            issueKey: issue.key,
            error: err.message
        };
    }
}

/**
 * Handle issue deleted events
 */
export async function onIssueDelete(event, context) {
    const { issue } = event;
    const cloudId = context.cloudId;

    console.log(`Issue delete event: ${issue.key}`, {
        cloudId
    });

    try {
        // Create minimal issue data for deletion
        const issueData = {
            key: issue.key,
            summary: '',
            description: null,
            status: 'closed',
            project_id: issue.fields?.project?.id || 'unknown',
            project_key: issue.fields?.project?.key || 'unknown',
            reporter_account_id: null,
            assignee_account_id: null,
            created: new Date().toISOString(),
            updated: new Date().toISOString(),
            resolved: null,
            labels: [],
            components: [],
            comments: []
        };

        // Send delete event to backend
        const result = await ingestSingle(issueData, cloudId, 'deleted');

        console.log(`Issue deleted from index: ${issue.key}`, result);

        return {
            success: true,
            issueKey: issue.key,
            status: 'deleted'
        };

    } catch (err) {
        console.error(`Failed to delete issue ${issue.key}:`, err.message);

        // FIXED: Throw for transient errors
        if (err.retryable || err.message.includes('503') || err.message.includes('timeout')) {
            throw new Error(`Transient failure deleting ${issue.key}: ${err.message}`);
        }

        return {
            success: false,
            issueKey: issue.key,
            error: err.message
        };
    }
}
