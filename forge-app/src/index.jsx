/**
 * Jira Cortex - Forge App Entry Point
 *
 * UI handlers and main exports.
 */

import Resolver from '@forge/resolver';
import { ContextPanel } from './components/ContextPanel';
import { OmniSearch } from './components/OmniSearch';
import { AdminSettings } from './components/AdminSettings';
import { ErrorBoundary } from './components/ErrorBoundary';

// Resolver for data fetching
const resolver = new Resolver();

/**
 * Context Panel Handler
 * Renders the AI assistant panel in issue sidebar
 */
export const contextPanelHandler = () => {
  return (
    <ErrorBoundary>
      <ContextPanel />
    </ErrorBoundary>
  );
};

/**
 * Omni Search Handler
 * Renders the global search/chat interface
 */
export const omniSearchHandler = () => {
  return (
    <ErrorBoundary>
      <OmniSearch />
    </ErrorBoundary>
  );
};

/**
 * Admin Settings Handler
 * Renders the admin configuration and sync page
 */
export const adminSettingsHandler = () => {
  return (
    <ErrorBoundary>
      <AdminSettings />
    </ErrorBoundary>
  );
};

// Export resolver
export const handler = resolver.getDefinitions();
