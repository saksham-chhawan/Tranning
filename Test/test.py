#!/usr/bin/env python3

import sys
import requests
from collections import defaultdict

BASE_URL = "https://api.github.com"

HEADERS = {
    "Accept": "application/vnd.github+json",
}

def authenticate(token):
    if token:
        HEADERS["Authorization"] = f"token {token}"

def get_user_repos(username):
    url = f"{BASE_URL}/users/{username}/repos"

    params = {
        "type": "public",
        "per_page": 100,
        "sort": "pushed",
        "direction": "ascending",
    }

    response = requests.get(url, headers=HEADERS, params=params)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error fetching repos: {response.status_code}")
        return []

def get_repo_languages(owner, repo_name):
    url = f"{BASE_URL}/repos/{owner}/{repo_name}/languages"

    response = requests.get(url, headers=HEADERS, timeout=5)

    if response.status_code == 200:
        return response.json()
    return {}

def aggregate_languages(repos, username):
    totals = defaultdict(int)

    for repo in repos:
        name = repo["name"]
        languages = get_repo_languages(username, name)

        for lang, bytes_count in languages.items():
            totals[lang] += bytes_count

    return totals

def display_results(totals):
    if not totals:
        print("No language data found.")
        return

    sorted_langs = sorted(totals.items(), key=lambda x: x[1], reverse=True)

    print("\n=== Language Usage (bytes) ===")
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