# QMapAnalyst

**QMapAnalyst** is a QGIS plugin for the cartometric analysis of historical maps. Inspired by the classic MapAnalyst software, it lets researchers, cartographers and historians measure the accuracy and local distortions of a historical map, estimate its paper scale and **the map projection it was most likely drawn in**, all inside QGIS.

## 🌟 Key features

* **Transformations:** Helmert (4 parameters), affine (6) and 2nd-order polynomial (12), all fitted by least squares.
* **Statistics:**
  * σ₀, mean position error, RMSE (total / X / Y), CE90/CE95 and error on paper (mm)
  * standard errors of scale, rotation and shear
  * **leave-one-out cross-validation** and the **Baarda outlier test**
  * comparison of transformation models with **AICc**
  * **Moran's I** spatial autocorrelation of residuals
  * **Tissot-based local distortion** (scale variation, rotation, angular distortion 2ω)
* **Map projection estimation:** fits 15 projections that matter for 15th–20th-century cartography to the control points and ranks them by AICc with Akaike weights. The projections are Plate carrée, equirectangular, Mercator, Cassini–Soldner, Gauss–Krüger, Sanson–Flamsteed, Bonne, trapezoidal, De l'Isle equidistant conic, Lambert conformal conic, Albers, Lambert azimuthal, stereographic, Postel and Hassler polyconic. Projection parameters (λ₀, φ₀, φ₁, φ₂) come with standard errors, and λ₀ is also given relative to the Ferro, Paris, Pulkovo and Warsaw meridians. The plugin can draw the estimated **graticule on the scan** for visual comparison.
* **Visualisation:** displacement vectors (arrows) and error circles on both maps, a TPS distortion grid with reliable and extrapolated zones, a warped reference grid on the scan, isolines of scale, rotation and angular distortion with labels, and a GCP layer that carries all statistics as attributes.
* **Export:**
  * full analysis report as a paginated PDF (header, page numbers), aligned plain text (TXT) or HTML, covering input data, all statistics, the residual table of every point, the projection ranking and hypotheses
  * a PDF with one map page per overlay
  * a georeferenced TIFF view
  * CSV tables of points and of the projection ranking
* **Workflow:**
  * continuous GCP capture and in-table coordinate editing
  * zoom to a point and synchronised views
  * the reference CRS is stored with the GCPs (`.points` / CSV)
  * automatic recomputation that refreshes existing result layers
* Works with QGIS 3.34+ (Qt5) and QGIS 4 (Qt6).
* **Bilingual:** the interface, the analysis report (panel and PDF), status and error messages, layer names and legends are available in Polish and English. The language follows the QGIS interface; any locale other than Polish uses English. Numbers in the Polish report use a decimal comma.

## 🚀 Installation

**From the QGIS Plugin Repository:** *Plugins → Manage and Install Plugins…*, search for `QMapAnalyst`.

**From ZIP:**
1. Run `python build.py` or download a release.
2. Open *Plugins → Manage and Install Plugins… → Install from ZIP* in QGIS.

## 📖 Workflow

1. **Historical map:** load an ungeoreferenced scan into the plugin's own map view.
2. **GCP tab:** capture point pairs by clicking on the scan and then on the QGIS main map, or load them from `.points` or CSV. The table shows the error, LOO error and standardized residual of each point.
3. **Analysis tab:** pick a transformation and the scan DPI, then read the report: accuracy, parameters with errors, map scale, outliers, model comparison, local distortion and autocorrelation.
4. **Projection tab:** estimate the map projection, inspect the ranking and its parameters, and draw the graticule on the scan.
5. **Layers tab:** generate result layers on the main map and on the scan.
6. **Export tab:** save a PDF report, TIFF and CSV files.

A detailed manual (in Polish) is available in [docs/manual_qmapanalyst.md](docs/manual_qmapanalyst.md).

### Notes on projection estimation

* Projections differ measurably only over larger areas. With points spanning less than about 2°, or with σ₀ differences under 10 %, the plugin warns that the result is only indicative.
* Gross errors in GCPs bias the projection parameters. The plugin flags suspect points; disable them and run the estimation again.
* The computations use a sphere. Geographic coordinates are derived from the project's reference CRS.
* The method follows the approach of T. Bayer, *Estimation of an unknown cartographic projection and its parameters from the map* (GeoInformatica, 2014), and B. Jenny's MapAnalyst.

## 🧪 Development

```bash
# engine tests (no GUI) – OSGeo4W shell
python-qgis.bat tests/test_engine.py
# full offscreen smoke test of the panel
set QT_QPA_PLATFORM=offscreen && python-qgis.bat tests/smoke_qgis.py
# regenerate translations (.ts/.qm)
python-qgis.bat i18n/build_translations.py
```

Module layout: `math_engine.py` (transformations and statistics), `projections.py` (projection estimation), `report.py` (HTML reports), `layers.py` (result layer registry and styles), `export.py` (PDF/CSV), `dockwidget.py` (UI).

## 📄 License

GNU General Public License v3.0 – see [LICENSE](LICENSE).

## 👤 Author

**Tomasz Nycz** – [GitHub](https://github.com/merkato)
*If you use this plugin in academic research, please consider citing it.*
