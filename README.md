# WLO Compound Planner

Pick an item from Wonderland Online and get a step-by-step compounding guide, starting from
NPC shop and gathered materials and ending at the target item.

Data comes from [wlohub.com](https://wlohub.com) (items, shops, compound formulas, gathering spots).

## Run on your PC
Double-click `start_app.bat`, or run `python wlo_app.py`. It opens in Chrome at http://127.0.0.1:8765/.

## Command line
```
python wlo_calc.py "Cloth Armband"          # writes guides/Cloth_Armband_1148.md
```

## Your own shortcut routes
Add tested recipes to `knowledge/shortcuts.json`. They count as confirmed and are always used
for the items they make.

## Refresh the data
```
pip install -r requirements-scraper.txt
python scrape_wlohub.py
```

## Hosting (Render)
`render.yaml` describes a free Render web service that runs `python wlo_app.py --cloud`.
