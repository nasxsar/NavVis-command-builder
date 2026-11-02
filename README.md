# NavVis Command Builder

A small page that writes `navvis-postprocessing` commands for you. You fill in a form, it gives you the command, you paste it into your terminal.

Try it here: https://nasxsar.github.io/NavVis-command-builder/

This is not an official NavVis tool and NavVis has nothing to do with it. More on that at the bottom.

## Why it exists

The post-processing command has a lot of flags, and the paths are long. If you work on Windows and run NavVis through WSL, you also end up converting `D:\Projects\...` to `/mnt/d/Projects/...` over and over. This does that part for you and keeps the flags in one place.

## What it does

It covers every option in the help output: time range, SLAM and anchors, point cloud settings, coloring, panoramas, image preset, and force.

Some things you might want to know:

- It only writes options you changed from the NavVis defaults. If you want every value spelled out, there's a checkbox for that.
- Options that don't apply get greyed out. Turn off panoramas and the panorama settings go grey, for example.
- Windows paths get converted to WSL paths. You can switch that off.
- Paths with spaces get quoted. A `*` wildcard is left alone so your shell expands it.
- `--cloud-format` always goes at the end. It takes several values, so if it sat in the middle it would eat your dataset paths.
- It warns you about obvious mistakes, like a stop time earlier than the start time, or a resolution outside 0.005 to 0.2.
- You can save setups under a name. They're stored in your own browser and nothing leaves your machine.


## Using it

it's simple Open the link above

## Things to keep in mind

I built the option list from one version of the NavVis help output (2026). Other versions might add or rename flags, so compare the command with `navvis-postprocessing --help` before you run anything important.

A few options only work on certain devices. `--preset` is for VLX and MLX, and `--cloud-mode` is for M6. The page shows a note about this but it can't tell which device recorded your data.

It only produces text. It doesn't run NavVis and it never touches your files.

## Rights

NavVis, VLX, MLX, M6, SiteMaker and `navvis-postprocessing` are names of products and software that belong to NavVis GmbH, and all rights in them stay with NavVis GmbH. I only use the option names so the commands come out right. There's no NavVis code or documentation text in here. The official documentation is at https://knowledge.navvis.com/.

original custom arguments source: https://knowledge.navvis.com/docs/custom-arguments

If something's wrong or missing, open an issue.
