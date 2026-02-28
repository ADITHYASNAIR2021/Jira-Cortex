/**
 * Jira Cortex - Forge App Entry Point
 *
 * UI handlers and main exports.
 */

import Resolver from '@forge/resolver';
import { ContextPanel } from './components/ContextPanel';
import { OmniSearch } from './components/OmniSearch';
import { AdminSettings } from './components/AdminSettings';

// Resolver for data fetching
const resolver = new Resolver();

/**
 * Context Panel Handler
 * Renders the AI assistant panel in issue sidebar
 */
export const contextPanelHandler = () => {
  return <ContextPanel />;
};

/**
 * Omni Search Handler
 * Renders the global search/chat interface
 */
export const omniSearchHandler = () => {
  return <OmniSearch />;
};

/**
 * Admin Settings Handler
 * Renders the admin configuration and sync page
 */
export const adminSettingsHandler = () => {
  return <AdminSettings />;
};

// Export resolver
export const handler = resolver.getDefinitions();
