name: Update repository analytics

on:
  schedule:
    - cron: "17 3 * * *"        # every day at 03:17 UTC
  workflow_dispatch:            # lets you run it manually from the Actions tab
  push:
    branches: [main]
    paths:
      - "scripts/update_analytics.py"
      - ".github/workflows/update-analytics.yml"

permissions:
  contents: write               # only needed to commit the generated files

concurrency:
  group: update-analytics
  cancel-in-progress: false

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Generate analytics
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}   # built-in token, no secret to create
          GH_USER: ${{ github.repository_owner }}
        run: python scripts/update_analytics.py

      - name: Commit only if something changed
        run: |
          if [ -z "$(git status --porcelain README.md assets/analytics)" ]; then
            echo "No changes to commit."
            exit 0
          fi
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add README.md assets/analytics
          git commit -m "chore: update repository analytics"
          git push
