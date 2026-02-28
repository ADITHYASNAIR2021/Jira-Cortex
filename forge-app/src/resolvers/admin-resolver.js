/**
 * Jira Cortex - Admin Resolver
 *
 * Resolver for Admin Settings page to call backend API.
 */

import Resolver from '@forge/resolver';
import { storage, getContext } from '@forge/api';
import { ingestBatch, getUsageStats, setBackendUrl, getJobStatus } from '../api/backend';

const resolver = new Resolver();

// Default backend URL
const DEFAULT_BACKEND_URL = 'https://jira-cortex-api.onrender.com';

/**
 * Get current backend URL
 */
resolver.define('getBackendUrl', async () => {
    try {
        const url = await storage.get('backendUrl');
        return url || DEFAULT_BACKEND_URL;
    } catch (err) {
        console.warn('Failed to get backend URL:', err);
        return DEFAULT_BACKEND_URL;
    }
});

/**
 * Ingest batch resolver - called by Admin Settings
 */
resolver.define('ingestBatch', async (req) => {
    const { issues } = req.payload;
    const context = await getContext();

    if (!issues || !Array.isArray(issues)) {
        throw new Error('Issues array is required');
    }

    if (issues.length === 0) {
        return { status: 'skipped', message: 'No issues to ingest' };
    }

    if (issues.length > 100) {
        throw new Error('Maximum 100 issues per batch');
    }

    try {
        const response = await ingestBatch(issues, context.cloudId);
        return response;
    } catch (err) {
        console.error('Ingest batch error:', err);
        throw new Error(err.message || 'Failed to ingest issues');
    }
});

/**
 * Get usage stats resolver
 */
resolver.define('getUsageStats', async () => {
    try {
        const stats = await getUsageStats();
        return stats;
    } catch (err) {
        console.warn('Usage stats error:', err);
        return {
            queries: 0,
            tokens: 0,
            cost: '0.00',
            error: err.message
        };
    }
});

/**
 * Configure backend URL (admin only)
 */
resolver.define('setBackendUrl', async (req) => {
    const { url } = req.payload;

    if (!url || typeof url !== 'string') {
        throw new Error('Valid URL is required');
    }

    // Basic URL validation
    try {
        new URL(url);
    } catch {
        throw new Error('Invalid URL format');
    }

    await setBackendUrl(url);
    return { success: true, url };
});

/**
 * Get job status for polling (UX fix)
 */
resolver.define('getJobStatus', async (req) => {
    const { jobId } = req.payload;

    if (!jobId) {
        throw new Error('Job ID is required');
    }

    try {
        const status = await getJobStatus(jobId);
        return status;
    } catch (err) {
        console.warn('Job status error:', err);
        return { status: 'unknown', error: err.message };
    }
});

export const adminResolver = resolver.getDefinitions();
