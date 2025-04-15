#!/bin/bash

set -e

REPO_URL="https://api.github.com/repos/yniverz/WayPointDB"
REPO_DIR="WayPointDB"

# Clone repo if missing
if [ ! -d "$REPO_DIR" ]; then
    git clone https://github.com/yniverz/WayPointDB.git
fi

cd "$REPO_DIR"

# Get current state
current_branch=$(git rev-parse --abbrev-ref HEAD)
current_commit=$(git rev-parse HEAD)
current_tag=$(git describe --tags --exact-match 2>/dev/null || true)

# Helper to confirm with user
confirm_proceed() {
    echo "⚠️  This operation may introduce breaking changes."
    read -p "Are you sure you want to continue? (y/N): " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "❌ Update canceled."
        exit 1
    fi
}

if [ "$1" == "--unreleased" ]; then
    echo "🛠  Updating to latest *unreleased* code from main branch..."

    # If switching from a tag or another branch, confirm
    if [[ "$current_tag" != "" || "$current_branch" != "main" ]]; then
        confirm_proceed
    fi

    git checkout main
    git pull origin main
else
    echo "📦 Fetching latest *release* tag..."
    latest_tag=$(curl -s "$REPO_URL/releases/latest" | grep -oP '"tag_name": "\K(.*)(?=")')

    if [ -z "$latest_tag" ]; then
        echo "❌ Failed to fetch the latest release tag."
        exit 1
    fi

    echo "✅ Latest release: $latest_tag"

    # If switching from a different tag or from main, confirm
    if [[ "$current_tag" != "$latest_tag" || "$current_branch" == "main" ]]; then
        confirm_proceed
    else
        echo "🔁 Already on $latest_tag, continuing with update..."
    fi

    git fetch --tags
    git checkout "$latest_tag"
fi

echo "🚧 Rebuilding and starting Docker containers..."
docker-compose down
docker-compose build
docker-compose up -d