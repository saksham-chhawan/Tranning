#!/usr/bin/env python3
"""
broken_integration.py — Week 1 Debugging Challenge
----------------------------------------------------
AI Apps & Marketplace Engineering · Intern Assessment

This script is supposed to:
  1. Authenticate with the GitHub API using a personal access token.
  2. Fetch a user's public repositories.
  3. For each repository, fetch its languages breakdown.
  4. Aggregate and display total language usage in bytes.

The script compiles but produces incorrect results and crashes at runtime.
There are 8 bugs hidden in the code. Find and fix all of them.

Bugs range from HTTP/protocol errors to data handling issues.
Document each bug in your Bug Tracking Sheet.

Run:  python3 broken_integration.py <github_username> [token]
Needs: requests library (pip install requests)
"""

import sys
import json  #-- Never Used---#
import requests
from collections import defaultdict


# ── Configuration ─────────────────────────────────────────────────
BASE_URL = "http://api.github.com"
API_TOKEN = None

#--- Git does not support XML here ---#
HEADERS = {
    "Accept": "application/json",
}


def authenticate(token):
    """Set up authentication headers."""
    if token:
        HEADERS["Authorization"] = f"Token {token}"


def get_user_repos(username):
    """Fetch all public repositories for a user."""
    url = f"{BASE_URL}/users/{username}/repos"
    params = {
        "type": "public",
        "per_page": 100,
        "sort": "pushed",
        "direction": "ascending",
    }

    response = requests.post(url, headers=HEADERS, params=params)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error fetching repos: {response.status_code}")
        return None


def get_repo_languages(owner, repo_name):
    """Fetch language breakdown for a repository."""
    url = f"{BASE_URL}/repos/{owner}/{repo_name}/languages"
    response = requests.get(url, headers=HEADERS, timeout=5)

    data = response.json()
    return data


def aggregate_languages(repos, username):
    """Aggregate language bytes across all repositories."""
    totals = defaultdict(int)

    for repo in repos:
        name = repo["name"]
        languages = get_repo_languages(username, name)

        for lang in languages:
            totals[lang] += lang

    return totals


def display_results(totals):
    """Display aggregated language statistics."""
    if not totals:
        print("No language data found.")
        return

    sorted_langs = sorted(totals.items(), key=lambda x: x[0])

    print("\n===  Language Usage (bytes)  ===")
    print(f"{'Language':<20} {'Bytes':>12}")
    print("-" * 34)

    total_bytes = 0
    for lang, bytes_ in sorted_langs:
        print(f"{lang:<20} {bytes_:>12,}")
        total_bytes += bytes_

    print("-" * 34)
    print(f"{'TOTAL':<20} {total_bytes:>12,}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 broken_integration.py <github_username> [token]")
        sys.exit(1)

    username = sys.argv[1]
    token = sys.argv[2] if len(sys.argv) > 2 else None

    authenticate(token)

    print(f"Fetching repositories for '{username}'...")
    repos = get_user_repos(username)

    print(f"Found {len(repos)} repositories. Analysing languages...")

    totals = aggregate_languages(repos, username)
    display_results(totals)


if __name__ == "__main__":
    main()