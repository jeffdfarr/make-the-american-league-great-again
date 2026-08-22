# Deploying MTALGA League HQ

The site is this folder: four HTML pages, assets/, and data/ (refreshed
nightly by the GitHub Actions sync). No build step — any web server that can
serve files can host it.

## Preview locally
    cd site
    python -m http.server 8000
    # open http://localhost:8000

## Deploy to your own hosting
Upload the CONTENTS of site/ (index.html, the other pages, assets/, data/)
to the web root (or a subdirectory — all paths are relative). Visiting the
URL serves index.html automatically.

## Keeping it fresh
The nightly GitHub Action commits updated site/data/*.json to the repo.
Getting those to your server, pick one:

1. **Server pulls (simplest if the server has git + ssh):** clone the repo on
   the server, point the web root at <repo>/site, add a cron entry:
       15 3 * * * cd /path/to/repo && git pull --quiet
2. **GitHub pushes:** add a step to .github/workflows/nightly.yml that
   rsyncs/FTPs site/ to the server after the sync commit (needs the server's
   SSH or FTP credentials as repo secrets — ask Claude to wire this once you
   know what access the server allows).
3. **Manual:** re-upload site/data/*.json whenever you want the site current.

## Notes
- data/ must be uploaded along with the pages — the site reads it at load time.
- Team logos hotlink from fantraximg.com (Fantrax's CDN).
- Google Fonts load from fonts.googleapis.com; if blocked, system fonts kick in.
