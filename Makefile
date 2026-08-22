# Installation und Auslieferung von Schwupp.
#
# Entwicklung:   make venv && make run
# Prüfen:        make check
# Installieren:  make install PREFIX=$HOME/.local     (ohne root, gut fürs Telefon)
# Flatpak:       make flatpak-build && make flatpak-publish   (siehe unten)

PREFIX  ?= /usr/local
DESTDIR ?=
APPID    = de.cais.Schwupp
PYTHON  ?= python3
VENV     = .venv
VERSION := $(shell cat VERSION)

.PHONY: help venv run check test lint install uninstall clean pip-modules

help:
	@echo "Schwupp $(VERSION) – wichtigste Ziele:"
	@echo "  make venv            Entwicklungsumgebung anlegen (.venv)"
	@echo "  make run             App aus dem Quellbaum starten"
	@echo "  make check           Tests + Linter + Metadaten prüfen"
	@echo "  make install         nach \$$PREFIX installieren (Standard: $(PREFIX))"
	@echo "  make flatpak-build   Flatpak für diese Architektur bauen"
	@echo "  make flatpak-publish Repo für die Auslieferung fertigstellen"
	@echo "  make release VERSION=x.y.z   Release taggen"

# --- Entwicklung ------------------------------------------------------------

venv:
	$(PYTHON) -m venv --system-site-packages $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt -e ".[dev]"
	@echo "Fertig. Starten mit: make run"

run:
	$(VENV)/bin/python -m app

test:
	$(VENV)/bin/python -m pytest

lint:
	$(VENV)/bin/python -m ruff check .

# Validiert Tests, Linter und die Metadaten-Dateien (soweit die Werkzeuge da sind).
check: test lint
	-desktop-file-validate data/$(APPID).desktop
	-appstreamcli validate --no-net data/$(APPID).metainfo.xml
	@$(PYTHON) -c "import json,sys; \
		de=set(json.load(open('lang/de.json'))); en=set(json.load(open('lang/en.json'))); \
		sys.exit('Sprachdateien weichen ab: %s' % (de ^ en) if de ^ en else 0)" \
		&& echo "Sprachdateien vollständig"

# --- Installation ins Dateisystem -------------------------------------------
# Nutzt pip, damit Starter, Paket und Datendateien (VERSION, lang/, .desktop,
# Metainfo, Icon) in einem Schritt an ihre XDG-Plätze kommen – dieselbe Route,
# die auch das Flatpak nimmt (siehe data-files in pyproject.toml).

install:
	$(PYTHON) -m pip install --no-deps --no-build-isolation \
		$(if $(DESTDIR),--root=$(DESTDIR),) --prefix=$(PREFIX) .
	@if [ -z "$(DESTDIR)" ]; then \
	  gtk4-update-icon-cache -f -t "$(PREFIX)/share/icons/hicolor" 2>/dev/null || true; \
	  update-desktop-database "$(PREFIX)/share/applications" 2>/dev/null || true; \
	fi
	@echo "Installiert nach $(PREFIX)."

uninstall:
	$(PYTHON) -m pip uninstall -y schwupp
	rm -f $(DESTDIR)$(PREFIX)/share/applications/$(APPID).desktop
	rm -f $(DESTDIR)$(PREFIX)/share/metainfo/$(APPID).metainfo.xml
	rm -f $(DESTDIR)$(PREFIX)/share/icons/hicolor/256x256/apps/$(APPID).png
	rm -rf $(DESTDIR)$(PREFIX)/share/schwupp

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# --- Python-Abhängigkeiten fürs Flatpak neu einfrieren -----------------------
# Erzeugt python3-modules.yaml (sha256-gepinnte Wheels/Sdists für x86_64 und
# aarch64) aus requirements.txt. Nur nötig, wenn sich Abhängigkeiten ändern.
# yt-dlp bleibt außen vor – das liefert das Manifest als eigene Binary mit.
FP_TOOLS = .flathub-tools

pip-modules:
	@test -x $(FP_TOOLS)/venv/bin/python || { \
		$(PYTHON) -m venv $(FP_TOOLS)/venv && \
		$(FP_TOOLS)/venv/bin/pip -q install requirements-parser PyYAML; }
	@grep -v '^\#' requirements.txt | grep -v '^yt-dlp' | grep . > $(FP_TOOLS)/req.txt
	$(FP_TOOLS)/venv/bin/python $(FP_TOOLS)/flatpak-pip-generator.py \
		--runtime='org.gnome.Sdk//49' --requirements-file=$(FP_TOOLS)/req.txt \
		--output python3-modules --yaml \
		--prefer-wheels cryptography,pydantic-core,aiohttp,cffi,zeroconf,propcache,multidict,yarl,frozenlist
	@# cffi separat, damit es im Manifest VOR miniaudio installiert wird.
	@printf 'cffi>=1.16\n' > $(FP_TOOLS)/req-cffi.txt
	$(FP_TOOLS)/venv/bin/python $(FP_TOOLS)/flatpak-pip-generator.py \
		--runtime='org.gnome.Sdk//49' --requirements-file=$(FP_TOOLS)/req-cffi.txt \
		--output python3-cffi --yaml --prefer-wheels cffi
	@echo "python3-modules.yaml und python3-cffi.yaml aktualisiert – bitte einchecken."

# ---------------------------------------------------------------------------
# Flatpak: ein OSTree-Repo als Update-Quelle, das BEIDE Architekturen enthält.
#
# Jede Architektur wird nativ gebaut und ins Repo committet:
#   x86_64 :  make flatpak-build                       (auf diesem Rechner)
#   aarch64:  auf dem Telefon `make flatpak-build`, das dort entstandene repo/
#             zurueckkopieren, dann hier `make flatpak-merge ARM_REPO=repo-arm`
# Danach einmal `make flatpak-publish` (Summary/AppStream/Deltas, optional
# signiert) und das Verzeichnis $(FP_REPO) per HTTPS hosten.
# ---------------------------------------------------------------------------
FP_MANIFEST ?= $(APPID).yaml
FP_REPO     ?= repo
FP_ARCH      = $(shell flatpak --default-arch)
FP_BUILDDIR ?= .flatpak-build/$(FP_ARCH)
FP_GPG      ?=
FP_GPGHOME  ?=
FP_GPGARGS   = $(if $(FP_GPG),--gpg-sign=$(FP_GPG) $(if $(FP_GPGHOME),--gpg-homedir=$(FP_GPGHOME)),)
# flatpak-builder als Host-Tool, sonst die geflatpakte Variante org.flatpak.Builder.
FP_BUILDER   = $(shell command -v flatpak-builder >/dev/null 2>&1 \
		&& echo flatpak-builder || echo flatpak run org.flatpak.Builder)

.PHONY: flatpak-build flatpak-install flatpak-merge flatpak-publish flatpak-pages flatpak-repo-info flatpak-gpg-key

# Baut die aktuelle Host-Architektur in $(FP_REPO).
flatpak-build:
	$(FP_BUILDER) --force-clean --repo=$(FP_REPO) $(FP_GPGARGS) \
		$(FP_BUILDDIR) $(FP_MANIFEST)
	@echo "$(FP_ARCH) liegt jetzt in $(FP_REPO)/. Refs: make flatpak-repo-info"

# Baut und installiert direkt für den angemeldeten Benutzer (zum Ausprobieren).
flatpak-install:
	$(FP_BUILDER) --force-clean --user --install $(FP_BUILDDIR) $(FP_MANIFEST)
	@echo "Starten mit: flatpak run $(APPID)"

# Fuehrt ein auf anderer Architektur gebautes Repo (ARM_REPO=<pfad>) zusammen.
flatpak-merge:
	@test -n "$(ARM_REPO)" || { echo "ARM_REPO=<pfad> angeben (das vom Telefon kopierte repo/)"; exit 1; }
	ostree --repo=$(FP_REPO) pull-local $(ARM_REPO)
	@echo "Zusammengefuehrt. Jetzt: make flatpak-publish"

# Signiert alle Commits im Repo nach und schreibt dann Summary/AppStream/
# Statische-Deltas.
#
# Wichtig: `flatpak build-sign` signiert ohne --arch nur die Architektur DIESES
# Rechners. Der auf dem Telefon gebaute aarch64-Commit bliebe sonst unsigniert –
# und weil `remote-info` nur die (signierte) Summary prüft, fällt das erst beim
# tatsächlichen Installieren auf: "GPG verification enabled, but no signatures
# found". Deshalb wird über alle Architekturen im Repo iteriert.
flatpak-publish:
ifneq ($(FP_GPG),)
	@for arch in $$(find $(FP_REPO)/refs/heads/app/$(APPID) -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null); do \
		echo "Signiere $(APPID) ($$arch)"; \
		flatpak build-sign $(FP_REPO) $(APPID) --arch=$$arch $(FP_GPGARGS) || exit 1; \
	done
endif
	@# Bestehende Deltas verwerfen: build-update-repo erzeugt nur fehlende neu.
	@# Ein vor dem Signieren gebautes Delta trägt den unsignierten Commit in sich –
	@# die Installation scheitert dann an "no signatures found", obwohl die
	@# Signatur im Repo liegt (ohne --no-static-deltas nicht zu sehen).
	rm -rf $(FP_REPO)/deltas $(FP_REPO)/delta-indexes
	flatpak build-update-repo --generate-static-deltas --prune $(FP_GPGARGS) $(FP_REPO)
	@echo "$(FP_REPO)/ ist fertig zum Hosten (per HTTPS ausliefern)."

# Schreibt Repo + .flatpakrepo + Landingpage in den gh-pages-Branch, von wo
# GitHub Pages sie ausliefert. Pusht nur mit `make flatpak-pages PUSH=1`.
flatpak-pages:
	scripts/publish-pages.sh $(if $(PUSH),--push,)

# Zeigt, welche App-Refs (Architekturen) aktuell im Repo liegen.
flatpak-repo-info:
	@ostree --repo=$(FP_REPO) refs 2>/dev/null | grep -E "^app/" | sort \
		|| echo "(noch kein $(FP_REPO) gebaut)"

# Erzeugt einen eigenen Signierschlüssel in einem projekteigenen GnuPG-Verzeichnis
# (nicht im persönlichen Schlüsselbund) und schreibt ihn in die .flatpakrepo.
GPGHOME = $(HOME)/.local/share/schwupp-flatpak
flatpak-gpg-key:
	@test ! -d $(GPGHOME) || { echo "$(GPGHOME) existiert bereits – nichts zu tun."; exit 0; }
	mkdir -p $(GPGHOME) && chmod 700 $(GPGHOME)
	gpg --homedir $(GPGHOME) --batch --passphrase '' --quick-generate-key \
		"Schwupp Flatpak Repo <flatpak@cais.de>" ed25519 sign never
	@echo
	@echo "Schlüssel liegt in $(GPGHOME). Bauen und signieren mit:"
	@echo "  make flatpak-build FP_GPG=\$$(gpg --homedir $(GPGHOME) --list-keys --with-colons | awk -F: '/^fpr/{print \$$10; exit}') FP_GPGHOME=$(GPGHOME)"

# ---------------------------------------------------------------------------
# Release schneiden:  make release VERSION=0.3.0
#
# Setzt die Version in der VERSION-Datei, verlangt einen passenden
# <release>-Eintrag in der Metainfo (die Notizen schreibst du vorher von Hand
# und committest sie) und taggt den Stand.
# ---------------------------------------------------------------------------
.PHONY: release
release:
	@echo "$(VERSION)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "VERSION=x.y.z angeben, z. B. make release VERSION=0.3.0"; exit 1; }
	@git diff-index --quiet HEAD -- || { echo "Arbeitsbaum nicht sauber – erst committen oder stashen."; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null || { echo "Tag v$(VERSION) existiert bereits."; exit 1; }
	@grep -q 'release version="$(VERSION)"' data/$(APPID).metainfo.xml || { echo "Kein <release version=\"$(VERSION)\"> in data/$(APPID).metainfo.xml – bitte zuerst die Release-Notiz ergänzen und committen."; exit 1; }
	echo "$(VERSION)" > VERSION
	git add VERSION
	git commit -q -m "Release: $(VERSION)"
	git tag "v$(VERSION)"
	@echo "✓ v$(VERSION) getaggt. Pushen:  git push && git push origin v$(VERSION)"
