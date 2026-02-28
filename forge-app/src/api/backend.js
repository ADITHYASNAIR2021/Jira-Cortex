/**
 * Jira Cortex - Backend API Client
 *
 * Secure communication with Python backend.
 * Handles JWT token generation and request signing.
 * 
 * FIXED: Uses Forge storage for backend URL configuration.
 */

import { fetch, storage } from '@forge/api';
import { getContext } from '@forge/api';

// Default backend URL - can be overridden via Forge storage
const DEFAULT_BACKEND_URL = 'https://jira-cortex-api.onrender.com';

/**
 * Get configured backend URL from Forge storage
 * Falls back to default if not configured
 */
async function getBackendUrl() {
    try {
        const configured = await storage.get('backendUrl');
        return configured || DEFAULT_BACKEND_URL;
    } catch (err) {
        console.warn('Failed to get backend URL from storage:', err.message);
        return DEFAULT_BACKEND_URL;
    }
}

/**
 * API Error class for structured error handling
 */
class CortexAPIError extends Error {
    constructor(message, statusCode, errorCode, retryable = false) {
        super(message);
        this.name = 'CortexAPIError';
        this.statusCode = statusCode;
        this.errorCode = errorCode;
        this.retryable = retryable;
    }
}

/**
 * Build authorization header for backend requests.
 * Uses Forge's built-in context which includes user permissions.
 */
async function getAuthHeader() {
    const context = await getContext();

    // Forge automatically provides a signed JWT that our backend can verify
    // This includes user identity and permissions
    return {
        'Authorization': `Bearer ${context.token}`,
        'Content-Type': 'application/json',
        'X-Tenant-ID': context.cloudId,
        'X-Account-ID': context.accountId
    };
}

/**
 * Make authenticated request to backend
 */
async function apiRequest(endpoint, options = {}) {
    const headers = await getAuthHeader();
    const backendUrl = await getBackendUrl();

    const url = `${backendUrl}${endpoint}`;

    try {
        const response = await fetch(url, {
            ...options,
            headers: {
                ...headers,
                ...options.headers
            }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));

            // Determine if error is retryable
            const retryable = response.status >= 500 || response.status === 429;

            throw new CortexAPIError(
                errorData.message || `Request failed with status ${response.status}`,
                response.status,
                errorData.error || 'UNKNOWN_ERROR',
                retryable
            );
        }

        return await response.json();

    } catch (error) {
        if (error instanceof CortexAPIError) {
            throw error;
        }

        // Network or other error - likely transient
        console.error('Backend request failed:', error.message);
        throw new CortexAPIError(
            'Unable to connect to Cortex backend',
            503,
            'CONNECTION_ERROR',
            true  // Network errors are retryable
        );
    }
}

/**
 * Query the knowledge base
 *
 * @param {string} query - User's natural language query
 * @param {Object} context - Optional current issue context
 * @returns {Promise<Object>} Query response with answer and citations
 */
export async function queryKnowledgeBase(query, context = null) {
    return apiRequest('/api/v1/query', {
        method: 'POST',
        body: JSON.stringify({
            query,
            context
        })
    });
}

/**
 * Ingest a batch of issues
 *
 * @param {Array} issues - Array of Jira issues to ingest
 * @param {string} tenantId - Tenant identifier
 * @returns {Promise<Object>} Ingestion job response
 */
export async function ingestBatch(issues, tenantId) {
    return apiRequest('/api/v1/ingest/batch', {
        method: 'POST',
        body: JSON.stringify({
            issues,
            tenant_id: tenantId,
            force_update: false
        })
    });
}

/**
 * Ingest a single issue (for webhook updates)
 *
 * @param {Object} issue - Jira issue to ingest
 * @param {string} tenantId - Tenant identifier
 * @param {string} eventType - Event type (created, updated, deleted)
 * @returns {Promise<Object>} Ingestion response
 */
export async function ingestSingle(issue, tenantId, eventType) {
    return apiRequest('/api/v1/ingest/single', {
        method: 'POST',
        body: JSON.stringify({
            issue,
            tenant_id: tenantId,
            event_type: eventType
        })
    });
}

/**
 * Get ingestion job status
 *
 * @param {string} jobId - Job ID to check
 * @returns {Promise<Object>} Job status
 */
export async function getJobStatus(jobId) {
    return apiRequest(`/api/v1/ingest/status/${jobId}`, {
        method: 'GET'
    });
}

/**
 * Get usage statistics for current tenant
 *
 * @returns {Promise<Object>} Usage stats
 */
export async function getUsageStats() {
    return apiRequest('/api/v1/usage/current', {
        method: 'GET'
    });
}

/**
 * Health check
 *
 * @returns {Promise<Object>} Backend health status
 */
export async function healthCheck() {
    return apiRequest('/health', {
        method: 'GET'
    });
}

/**
 * Set backend URL (admin only)
 *
 * @param {string} url - New backend URL
 */
export async function setBackendUrl(url) {
    await storage.set('backendUrl', url);
}

export { CortexAPIError };
