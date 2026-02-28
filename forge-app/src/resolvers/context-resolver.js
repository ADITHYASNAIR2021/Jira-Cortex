/**
 * Jira Cortex - Context Resolver
 *
 * Resolver for UI components to call backend API.
 */

import Resolver from '@forge/resolver';
import { queryKnowledgeBase, ingestBatch, getUsageStats } from '../api/backend';
import { getContext } from '@forge/api';

const resolver = new Resolver();

/**
 * Query resolver - called by UI components
 */
resolver.define('queryResolver', async (req) => {
    const { query, context, sessionId } = req.payload;

    if (!query || typeof query !== 'string') {
        throw new Error('Query is required');
    }

    if (query.length < 3) {
        throw new Error('Query must be at least 3 characters');
    }

    if (query.length > 2000) {
        throw new Error('Query must be less than 2000 characters');
    }

    try {
        const response = await queryKnowledgeBase(query, context, sessionId);
        return response;
    } catch (err) {
        console.error('Query resolver error:', err);

        // Return user-friendly error
        if (err.statusCode === 401) {
            throw new Error('Authentication failed. Please refresh the page.');
        }

        if (err.statusCode === 503) {
            throw new Error('Cortex service is temporarily unavailable. Please try again.');
        }

        throw new Error(err.message || 'Failed to process query');
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

export const contextResolver = resolver.getDefinitions();
