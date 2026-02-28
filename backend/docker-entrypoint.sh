#!/bin/sh
set -e

echo "=== Jira Cortex API Startup ==="

# Run database migrations if DATABASE_URL is set
if [ -n "$DATABASE_URL" ]; then
    echo "Running database migrations..."
    alembic upgrade head || echo "Warning: Migrations failed or not configured"
fi

# Start the application
echo "Starting application..."
exec "$@"
