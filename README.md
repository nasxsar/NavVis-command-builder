# NavVis post-processing command builder

A single-file web tool that builds `navvis-postprocessing` commands from a form.

- Covers every option in the NavVis post-processing help output
- Writes only non-default values (or all of them, if you choose)
- Converts Windows paths (`C:\...`) to WSL paths (`/mnt/c/...`)
- Quotes paths with spaces and keeps `--cloud-format` last so it can't swallow dataset paths
- Saves named setups in your browser

## Use

Open `index.html` in a browser. No build step, no dependencies (fonts load from Google Fonts and fall back to system fonts offline).

To host it: enable GitHub Pages on the repository (Settings → Pages → Deploy from branch → `main` / root).

Check the options against your installed NavVis version with `navvis-postprocessing --help`.
