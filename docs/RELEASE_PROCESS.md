# FableGear Release Process

## Automated Release System

FableGear uses GitHub Actions to automatically build and attach distributable `.zip` files to every release. This ensures your website download link always works:

```url
https://github.com/fabledharbinger0993/FableGear/releases/latest/download/FableGear.zip
```

## Creating a Release

### Method 1: Automated (Recommended)

```bash
# Tag the current commit
git tag -a v2.2.6 -m "Release v2.2.6 - Description of changes"

# Push the tag
git push origin --tags

# Use build script to create GitHub release
bash build_release.sh --release
```

The `--release` flag automatically:

1. Creates a GitHub release for the tag
2. Uploads `FableGear.zip` (the workflows will also run and attach both zips)
3. Generates release notes with installation instructions

### Method 2: Manual via GitHub Web UI

1. Go to: <https://github.com/fabledharbinger0993/FableGear/releases/new>
2. Choose existing tag or create new tag (e.g., `v2.2.6`)
3. Write release title and notes
4. Click **Publish release**

GitHub Actions will automatically:

- Build `FableGear.app` from source
- Build `FableGear Agent.app` from source
- Create both `.zip` files
- Attach them to the release

## What Happens Automatically

When you publish a release, two GitHub Actions workflows run in parallel:

### `release-zip.yml` — Main FableGear

1. Checks out the tagged commit
2. Builds `FableGear.app` bundle (launcher script + Info.plist)
3. Creates `FableGear.zip`
4. Uploads to release with `--clobber` (replaces if exists)

### `release-agent-zip.yml` — Agent Variant

1. Checks out the tagged commit
2. Builds `FableGear Agent.app` bundle
3. Creates `FableGear-Agent.zip`
4. Uploads to release with `--clobber`

## Download Links

Once workflows complete (takes ~30 seconds), these links work automatically:

- **Latest FableGear**: `https://github.com/fabledharbinger0993/FableGear/releases/latest/download/FableGear.zip`
- **Latest Agent**: `https://github.com/fabledharbinger0993/FableGear/releases/latest/download/FableGear-Agent.zip`
- **Specific version**: Replace `/latest/` with `/download/v2.2.6/` for a specific tag

## Versioning

- Use semantic versioning: `v<major>.<minor>.<patch>`
- Examples: `v2.2.5`, `v2.3.0`, `v3.0.0`
- The tag becomes the version in `Info.plist` automatically

## Testing a Release

After creating a release, verify:

1. **Workflows succeeded**: Check <https://github.com/fabledharbinger0993/FableGear/actions>
2. **Zips attached**: Visit release page, confirm both zips are listed
3. **Download works**: Click each zip, verify they download
4. **App launches**: Unzip, double-click the app, verify it opens Terminal and clones repo

## Troubleshooting

**Workflow fails with "permission denied"**

- Check that `permissions: contents: write` is set in workflow YAML
- Ensure GITHUB_TOKEN has release write permissions

**Zip missing from release**

- Check workflow run logs in Actions tab
- Verify the workflow trigger is `on: release: types: [published]`
- Confirm tag was pushed before creating release

**Download link 404**

- GitHub Actions may still be running (~30 seconds)
- Check release page to see if "Assets" section shows the zips
- Verify workflow completed successfully in Actions tab

## Build Scripts (Local Development)

For local testing before creating a release:

```bash
# Build FableGear.zip (creates it in current directory)
bash build_release.sh

# Build FableGear-Agent.zip
bash build_agent_release.sh
```

These scripts create the same `.app` bundles that GitHub Actions builds, but locally. Useful for testing the bootstrap launcher before cutting a release.

## Why .app Files Aren't Committed

`packaging/*.app` is gitignored because:

- Binary files bloat git history
- GitHub Actions builds them from source on every release
- Keeps repo size small
- Ensures reproducible builds from launcher scripts

The workflows embed the launcher scripts directly (via heredoc), so everything is built from text source files in the repo.
