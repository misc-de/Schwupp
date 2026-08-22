#!/usr/bin/env bash
# Legt den Inhalt des OSTree-Repos in den gh-pages-Branch, von wo GitHub Pages
# ihn unter https://misc-de.github.io/Schwupp/ ausliefert.
#
#   scripts/publish-pages.sh            # gh-pages lokal bauen und committen
#   scripts/publish-pages.sh --push     # zusätzlich pushen
#
# Der Branch wird bei jedem Lauf neu geschrieben (ein Commit, keine Historie) –
# ein OSTree-Repo ist ein Cache, keine Versionsgeschichte. Das Arbeitsverzeichnis
# des aktuellen Branches bleibt unangetastet: gebaut wird in einem Worktree.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

repo_dir="${FP_REPO:-repo}"
branch="gh-pages"
worktree="$(mktemp -d)"
trap 'git worktree remove --force "$worktree" 2>/dev/null || true; rm -rf "$worktree"' EXIT

[[ -d "$repo_dir/objects" ]] || { echo "Kein OSTree-Repo in $repo_dir/ – erst 'make flatpak-build' und 'make flatpak-publish'." >&2; exit 1; }

refs=$(find "$repo_dir/refs/heads/app" -type f 2>/dev/null | sed "s|$repo_dir/refs/heads/||" || true)
[[ -n "$refs" ]] || { echo "Im Repo liegen keine App-Refs." >&2; exit 1; }
echo "Wird veröffentlicht:"; echo "$refs" | sed 's/^/  /'

# Leeren Worktree für den gh-pages-Branch anlegen (Branch ggf. neu erzeugen).
if git show-ref --verify --quiet "refs/heads/$branch"; then
    git worktree add --force "$worktree" "$branch" >/dev/null
    git -C "$worktree" rm -rq . 2>/dev/null || true
else
    git worktree add --force --detach "$worktree" >/dev/null
    git -C "$worktree" checkout --orphan "$branch" >/dev/null 2>&1
    git -C "$worktree" rm -rq --cached . 2>/dev/null || true
    find "$worktree" -mindepth 1 -maxdepth 1 -not -name .git -exec rm -rf {} +
fi

# Jekyll würde Verzeichnisse mit führendem Unterstrich verschlucken.
touch "$worktree/.nojekyll"
cp data/de.cais.Schwupp.flatpakrepo data/de.cais.Schwupp.gpg "$worktree/"
cp -r "$repo_dir" "$worktree/repo"

cat > "$worktree/index.html" <<'HTML'
<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Schwupp – Flatpak-Repo</title>
<style>body{font-family:system-ui,sans-serif;max-width:46rem;margin:3rem auto;padding:0 1rem;line-height:1.5}
code,pre{background:#f4f4f4;border-radius:4px}pre{padding:1rem;overflow:auto}code{padding:.1rem .3rem}</style>
</head><body>
<h1>Schwupp</h1>
<p>Bildschirm, YouTube und Medien auf Chromecast, LG&nbsp;webOS, AirPlay&nbsp;2 und DLNA casten –
GTK4/libadwaita, Desktop und Phosh. Flatpak-Repo für <b>x86_64</b> und <b>aarch64</b>, signiert.</p>
<h2>Installieren</h2>
<pre>flatpak remote-add --if-not-exists schwupp https://misc-de.github.io/Schwupp/de.cais.Schwupp.flatpakrepo
flatpak install schwupp de.cais.Schwupp
flatpak run de.cais.Schwupp</pre>
<p>Updates danach mit <code>flatpak update</code>. Quellcode: <a href="https://github.com/misc-de/Schwupp">github.com/misc-de/Schwupp</a>.</p>
</body></html>
HTML

git -C "$worktree" add -A
if git -C "$worktree" diff --cached --quiet; then
    echo "Keine Änderungen – gh-pages ist bereits aktuell."
else
    git -C "$worktree" commit -q -m "Flatpak-Repo aktualisiert ($(cat VERSION))"
    echo "gh-pages committet."
fi

if [[ "${1:-}" == "--push" ]]; then
    git push -f origin "$branch"
    echo "Gepusht. In den Repo-Einstellungen muss GitHub Pages auf den Branch gh-pages (Ordner /) zeigen."
else
    echo "Noch nicht gepusht. Wenn alles passt:  git push -f origin $branch"
fi
