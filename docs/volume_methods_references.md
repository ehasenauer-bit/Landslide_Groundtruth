# Volume estimates — methods, papers, and why each one is here

Every number the Volume tab can print, the equation behind it, the paper it
comes from, and the reason that method is (or is no longer) the one to reach
for. Written 2026-09-21.

Where each one stands:

* **§1, Larsen et al. (2010) area–volume scaling `V = αA^γ` — the previous
  default.** Still in the plugin, but no longer the method to quote for an
  Alaska rock/ice avalanche: the calibration does not cover ice.
* **§2, `∫Δh` over a differenced DEM — the best measurement, when it is
  available.** It measures the event instead of inferring it. In practice two
  DEM epochs bracketing one of these events usually do not exist, so it cannot
  be the thing the tab opens on.
* **§3, deposit area × thickness — what the tab now defaults to.** The only fit
  that runs on an outline alone. Read §3's second half before trusting it on a
  small event, and §7 for what a better version would look like.

---

## 0. The four Fits at a glance

| Fit (`volume_calc.FITS`) | Equation | Needs | Primary source |
|---|---|---|---|
| Source scar | `V = αA^γ` | one polygon | Larsen et al. 2010 |
| Total landslide area | `V = αA^γ`, different α,γ | one polygon + coefficients you supply | Larsen et al. 2010 (constants not shipped) |
| Elevation change (`∫Δh`) | `V = Σ Δh·px²` | pre/post DEM or an imported Δh | Nuth & Kääb 2011 (co-registration); Bessette-Kirton et al. 2018 (the glacier case) |
| Deposit area × thickness | `V = A·t̄` | one polygon + a thickness | Toney et al. 2021 |

A fifth number travels with the AEC detection record but is not computed here:
the **seismic volume** (§4, Ekström & Stark 2013), used only as a cross-check.

---

## 1. Area–volume scaling — *the previous method*

### The paper

> Larsen, I. J., Montgomery, D. R., & Korup, O. (2010). Landslide erosion
> controlled by hillslope material. **Nature Geoscience 3**, 247–251.
> doi:[10.1038/ngeo776](https://doi.org/10.1038/ngeo776) ·
> [free author PDF](https://gis.ess.washington.edu/grg/publications/pdfs/Larsen_et_al_2010.pdf)

The coefficients used are **Table S1** of the supplement, the *scar* (source
area) fits. Compiled from 4,231 individual landslides.

### How the calculation works

The relation is a power law fitted as a straight line in log₁₀ space:

```
log₁₀V = log₁₀α + γ · log₁₀A          V = α · A^γ
```

Coefficients as shipped ([`volume_calc.py:28`](../qgis_plugin/landslide_groundtruth/volume_calc.py#L28),
mirroring [`larsen_BR_volume.py:32`](../larsen_BR_volume.py#L32)):

| material | log₁₀α | σ(log₁₀α) | γ | σ(γ) |
|---|---|---|---|---|
| bedrock | −0.63 | 0.06 | 1.41 | 0.02 |
| soil | −0.649 | 0.021 | 1.262 | 0.009 |

Because γ > 1, volume grows faster than area — deeper failures for bigger
scars, which is the physical content of the fit.

**Uncertainty** is propagated in log space
([`_coefficient_volume`, volume_calc.py:106](../qgis_plugin/landslide_groundtruth/volume_calc.py#L106)):

```
σ²(log₁₀V) = σ²(log₁₀α) + (log₁₀A)²·σ²(γ) + γ²·σ²(log₁₀A)
V_low, V_high = 10^(log₁₀V ∓ σ(log₁₀V))
```

The three terms are: where the intercept sits, where the slope sits (amplified
by how far the scar is from A = 1 m², so **the σ widens with scar size**), and
your own area uncertainty. That last term is zero unless you assign low/high
source outlines; when you do, `log₁₀A_low … log₁₀A_high` is read as a **±2σ**
span, so σ(log₁₀A) = mean(d₁,d₂)/2 — a deliberately conservative reading of two
hand-digitised polygons, and the reason a narrow low/high pair tightens the
range faster than you may intend.

**Worked example** — a 0.5 km² bedrock scar:

```
log₁₀A = 5.699                    V       = 25.4 Mm³
log₁₀V = −0.63 + 1.41(5.699)      range   = 18.9 – 34.2 Mm³   (−26% / +35%)
       = 7.406                    implied mean depth = 50.9 m
```

The same polygon called **soil** gives 3.5 Mm³ — 7.3× smaller. Across the sizes
you actually digitise the bedrock/soil ratio runs ~4× (1 ha) to ~8× (1 km²).
**The material toggle moves the answer by far more than the ± the tab prints.**

### Why it was used

It needs nothing but a polygon. No DEM, no second epoch, no radar. For a
freshly-detected event where the only thing you have is one clear optical scene
and a scar you can trace, it is the only method on the list that runs at all,
and it is the community-standard relation for doing so.

### Why it is no longer what to quote here

1. **The printed range is the wrong interval.** It propagates uncertainty in
   *where the regression line sits*, not how far individual landslides scatter
   *about* that line. The predictive scatter in Larsen's own data is roughly a
   factor of 2–3 (σ ≈ 0.40 in log₁₀ ⇒ ×0.40 to ×2.5) — several times wider than
   the ±26%/+35% above. The tab's tooltip says this; the number still reads
   more confident than it is.
2. **Larsen's own σ is ambiguous.** The main text calls it a standard
   deviation, the supplement calls it a standard error. `larsen_BR_volume.py`
   assumes SD, because SE gives error bars that are implausibly large after
   propagation — documented at [`larsen_BR_volume.py:11`](../larsen_BR_volume.py#L11).
3. **Material choice dominates** (factor 4–8, above), and for a mixed rock/ice
   failure neither option is right.
4. **It is calibrated on source-scar area.** Running a total-area outline
   through the scar fit reads high. This is why `LARSEN_TOTAL` ships
   **empty** ([`volume_calc.py:55`](../qgis_plugin/landslide_groundtruth/volume_calc.py#L55))
   and the tab refuses rather than reusing the scar coefficients on a larger
   polygon — the published total-area constants are not reproduced because
   guessing them would be worse than refusing.
5. **The Alaska case is outside the calibration.** Larsen's populations are
   soil and bedrock *hillslope* failures. A rock-and-ice avalanche detaching
   from a glacierised headwall, entraining snow and ice and running out onto a
   glacier, is in neither population. This is the case the plugin exists for,
   and it is the case the relation does not cover.

---

## 2. `∫Δh` — elevation change over the outline — *the best measurement*

### The equation

```
V_net = Σ Δh · px²           over valid pixels inside the outline
V_deposit = Σ (Δh > 0) · px²        V_erosion = Σ (Δh < 0) · px²
```

[`integrate_dh`, dem_diff.py:396](../qgis_plugin/landslide_groundtruth/dem_diff.py#L396).
Erosion and deposition are reported separately because their sum is the *net*
change and their difference is the mass-balance check.

### The geodesy, and the paper behind it

> Nuth, C., & Kääb, A. (2011). Co-registration and bias corrections of
> satellite elevation data sets for quantifying glacier thickness change.
> **The Cryosphere 5**, 271–290.
> doi:[10.5194/tc-5-271-2011](https://doi.org/10.5194/tc-5-271-2011) ·
> [open-access PDF](https://tc.copernicus.org/articles/5/271/2011/tc-5-271-2011.pdf)

Three things have to be true before a difference of two DEMs is a volume:

* **A common datum.** Both PGC SETSM s2s041 strips carry heights on the WGS84
  *ellipsoid*, so differencing cancels the geoid exactly — no datum work.
* **No vertical bias.** Each strip carries up to a few metres of absolute
  vertical error from satellite geolocation. Over an AOI that is *mostly*
  stable, that bias is the robust central tendency of the difference map, so
  [`coregister_offset`, dem_diff.py:112](../qgis_plugin/landslide_groundtruth/dem_diff.py#L112)
  estimates it with a σ-clipped median (3σ, 5 iterations) and subtracts it.
  This is the **vertical piece of Nuth & Kääb's step (i)**; the horizontal
  (aspect/slope) piece is skipped because s2s041 strips are already registered
  well under the resolutions used here.
  σ is the MAD × 1.4826 of the surviving stable pixels.
* **No fake change.** The strip `bitmask` flags edge, water and cloud pixels.
  Differencing unmasked water or cloud produces spectacular fake elevation
  change, so the mask is applied by default.

For an **imported** Δh (one this plugin did not compute), the same estimate is
made from ground *outside* the outline —
[`stable_ground_stats`, dem_diff.py:350](../qgis_plugin/landslide_groundtruth/dem_diff.py#L350) —
with the outline dilated 3 px first, because the deposit usually runs past the
digitised scar and would otherwise drag the "stable" median toward the event.
**Volume is linear in that offset:** 0.5 m of residual bias over a 1 km²
outline integrates to 500,000 m³ of pure artefact.

### Volume uncertainty

Two bounds, differing by √N — for a 1 km² outline at 10 m that is a factor of
10, so quoting the wrong one is not a detail:

```
correlated   σ_V = σ_h · A            ← reported as v_sigma_m3
random       σ_V = σ_h · px · √N      ← carried alongside
```

The **correlated** bound is the one reported, because the realistic error in
these inputs is a DC or long-wavelength field, not white noise. The random
bound is kept for a caller with a genuinely uncorrelated product.

A **coverage fraction** is also returned: a Δh layer overlapping 40 % of the
slide used to return 40 % of the volume with nothing said — indistinguishable
from a small landslide.

### Why this is the method to use

It measures *this* event rather than inferring it from an empirical population.
No material assumption, no calibration range to fall outside of, and it works
on ice — which is exactly where §1 fails. Erosion and deposition come out
separately, so source and deposit can be checked against each other.

### What to watch

> Bessette-Kirton, E. K., Coe, J. A., & Zhou, W. (2018). Using stereo satellite
> imagery to account for ablation, entrainment, and compaction in volume
> calculations for rock avalanches on glaciers: application to the 2016
> Lamplugh rock avalanche, Glacier Bay National Park, Alaska.
> **JGR Earth Surface 123**(4), 622–641.
> doi:[10.1002/2017JF004512](https://doi.org/10.1002/2017JF004512)

This is the paper on the failure mode specific to our setting. Between two
epochs a glacier **ablates**, the avalanche **entrains** ice and snow, and the
deposit **compacts** — so a raw Δh over a glacier is not the rock volume. Their
Lamplugh result splits ~70 Mm³ into 51.7 Mm³ rock + 13.2 Mm³ entrained ice. The
plugin's `∫Δh` returns the *mobilised* volume and does not attempt that split;
where the surrounding ground is itself a moving glacier, the stable-ground
offset is also being drawn from moving ground, which is why the stable-pixel
count is reported in the log.

---

## 3. Deposit area × mean thickness — *the tab's default*

### The paper

> Toney, L., Fee, D., Allstadt, K. E., Haney, M. M., & Matoza, R. S. (2021).
> Reconstructing the dynamics of the highly similar May 2016 and June 2019
> Iliamna Volcano (Alaska) ice–rock avalanches from seismoacoustic data.
> **Earth Surface Dynamics 9**, 271–293.
> doi:[10.5194/esurf-9-271-2021](https://doi.org/10.5194/esurf-9-271-2021)
> (open access)

### The calculation

```
V = plan area × mean thickness          V (Mm³) = A (km²) × t (m)
```

[`volume_thickness`, volume_calc.py:212](../qgis_plugin/landslide_groundtruth/volume_calc.py#L212).
There is no fitted coefficient and therefore no propagated σ: the low/high
thickness is carried straight through, because **the mean thickness *is* the
dominant uncertainty** and burying it in a coefficient would hide that.

Toney et al. assume **1.5 ± 1 m uniformly over the slope**, giving
(13±8)×10⁶ m³ for 2016 and (11±7)×10⁶ m³ for 2019. They say plainly that the
masses are not well constrained because the deposit thickness is not.

### Why 1.5 m is defensible

The assumption traces back to prior Red Glacier deposits described as "a few
metres thick":

> Waythomas, C. F., Miller, T. P., & Beget, J. E. (2000). Record of late
> Holocene debris avalanches and lahars at Iliamna Volcano, Alaska.
> **JVGR 104**(1–4), 97–130.
> doi:[10.1016/S0377-0273(00)00202-X](https://doi.org/10.1016/S0377-0273(00)00202-X)

> Huggel, C., Caplan-Auerbach, J., Waythomas, C. F., & Wessels, R. (2007).
> Monitoring and modeling ice-rock avalanches from ice-capped volcanoes: a case
> study of frequent large avalanches on Iliamna Volcano, Alaska.
> **JVGR 168**(1–4), 114–136.
> doi:[10.1016/j.jvolgeores.2007.08.009](https://doi.org/10.1016/j.jvolgeores.2007.08.009)

and is independently confirmed by the one supraglacial sheet that was actually
*measured*:

> Shreve, R. L. (1966). Sherman landslide, Alaska. **Science 154**(3757),
> 1639–1643. doi:[10.1126/science.154.3757.1639](https://doi.org/10.1126/science.154.3757.1639)
> — 10.1 Mm³ over 8.25 km² = **1.65 m** mean.

### How far 1.5 m generalises — read this before trusting a small event

`deposit_thickness_card.html` §3 claims the mean stays ~1.5–1.7 m "fairly
independent of volume" across ~1–15 Mm³. **That is not supported by the sources
cited there**, and an earlier draft of this file repeated it. What those sources
actually give is two points, both near 10 Mm³:

| Event | Volume | Mean thickness | Status |
|---|---|---|---|
| Sherman 1964 | 10.1 Mm³ over 8.25 km² | 1.65 m | **measured** (Shreve 1966) |
| Iliamna 2016/2019 | 11–13 Mm³ | 1.5 m | **assumed**, from the Red Glacier literature |

One measurement and one assumption — and not independent of each other, since
Toney et al.'s 1.5 m traces to the same Red Glacier work. Together they
constrain ~10 Mm³ and say nothing about 1 Mm³.

Two things argue against extrapolating downward:

* **The one measured small supraglacial deposit is far thinner.** The 2010
  Brenndalsbreen rock avalanche onto a Norwegian glacier — deposit
  0.130 ± 0.065 Mm³ — used a mean sediment thickness of **0.40 ± 0.20 m**, an
  order of magnitude below the card's "a ~1 Mm³ event is still ~1–2 m". Read it
  as a lower bound: the authors note the thickness was hard to constrain because
  the fieldwork came a decade after the event, once fines had washed out. *(Taken
  from the paper's abstract/index entry, not verified against the full text.)*

  > Engen, S. H., Gjerde, M., Scheiber, T., Seier, G., Elvehøy, H., Abermann, J.,
  > Nesje, A., Winkler, S., Haualand, K. F., Rüther, D. C., Maschler, A., &
  > Yde, J. C. (2024). Investigation of the 2010 rock avalanche onto the
  > regenerated glacier Brenndalsbreen, Norway. **Landslides**.
  > doi:[10.1007/s10346-024-02275-z](https://doi.org/10.1007/s10346-024-02275-z)

* **The field does not consider the scaling solved.** The largest Alaska
  supraglacial inventory — 69 rock avalanches in Glacier Bay, 1984–2020 —
  never converts to volume at all. It reports deposit **area** throughout, and
  states outright that "the derivation of volume from area is poorly
  constrained", citing the erosion/entrainment/compaction problem of
  Bessette-Kirton et al. (2018). It assumes no thickness — the word appears
  twice in the paper, neither time as a measurement — and closes by listing
  more robust area–volume scaling as future work. Had a near-constant
  supraglacial thickness been established, that paper would have used one.

  Its area distribution is the useful part for this plugin, since it is the
  population our events are drawn from:

  | | Deposit area |
  |---|---|
  | Mean, all 69 RAs (1984–2020) | **1.16 ± 2.9 km²** |
  | Mean, 1984–2016 combined inventory | 1.34 ± 3.29 km² |
  | Mean, the 27 they newly detected | 0.49 ± 0.33 km² |
  | Range | 0.097 km² → 22.19 km² (2016 Lamplugh) |
  | Detection floor | 0.05 km² |
  | Share under 0.5 km² | ~71 % |

  At the default 1.5 m, a mean 1.16 km² deposit implies ~1.7 Mm³ — an order of
  magnitude below the ~10 Mm³ where the 1.5 m anchor is actually calibrated.
  **The typical Alaska supraglacial event sits in exactly the size range where
  the thickness is least supported.**

  The paper also notes that glacially-deposited rock avalanches spread further
  for a given volume than non-glacial ones (after Sosio et al. 2012), which it
  invokes to explain the roll-off at large sizes in its frequency–area
  distribution. That is a mechanism for deposits being *thinner* than a
  constant-thickness assumption predicts, and it argues against the card's
  "larger events run thicker" as much as against a constant.

  > Smith, W. D., Dunning, S. A., Ross, N., Telling, J., Jensen, E. K.,
  > Shugar, D. H., Coe, J. A., & Geertsema, M. (2023). Revising supraglacial
  > rock avalanche magnitudes and frequencies in Glacier Bay National Park,
  > Alaska. **Geomorphology 425**, 108591.
  > doi:[10.1016/j.geomorph.2023.108591](https://doi.org/10.1016/j.geomorph.2023.108591)
  > (open access)

**Upshot.** Treat 1.5 m as an *anchor at ~10 Mm³*, not a constant. Larger or
confined events run thicker — Lamplugh several m, Taan Fiord variable:

> Higman, B., et al. (2018). The 2015 landslide and tsunami in Taan Fiord,
> Alaska. **Scientific Reports 8**, 12993.
> doi:[10.1038/s41598-018-30475-w](https://doi.org/10.1038/s41598-018-30475-w)
> (open access) — ~60 Mm³, variable thickness, ran into a fjord.

Well below ~10 Mm³ there is no published support for 1.5 m. Carry a much wider
low/high there, and say so in the verdict rather than printing a tight range.

The ready-reckoner is [`deposit_thickness_card.html`](deposit_thickness_card.html),
with the caveat above.

### Why it is here

It is the only method that needs neither a second DEM epoch nor a calibration
the event fits inside. For an Iliamna-class supraglacial sheet it is honest
about being an order-of-magnitude estimate, and its one free parameter is
visible on screen instead of hidden in a regression.

---

## 4. Seismic volume — the independent cross-check

> Ekström, G., & Stark, C. P. (2013). Simple scaling of catastrophic landslide
> dynamics. **Science 339**(6126), 1416–1419.
> doi:[10.1126/science.1232887](https://doi.org/10.1126/science.1232887) ·
> [PDF](https://wpg.forestry.oregonstate.edu/sites/default/files/seminars/Ekstrom_2013_Landslide%20seismic.pdf)

Long-period seismic waves are inverted for the **force history** the landslide
applied to the Earth; with geometric constraints from imagery this yields
momentum, duration and **mass**. It needs no polygon at all, which is what makes
it a genuinely independent check on everything above.

**The trap:** a seismic volume is a *mass ÷ an assumed density*. Before
comparing it against an area-scaling or ∫Δh volume, check both assume the same
material. Bulk ≈ 1710 kg/m³ for a 50/50 ice–rock mix, ~2500 for mostly rock,
~920 for mostly ice — a factor of 2.7 across that span. The Volume tab's
material note says this next to the bedrock/soil toggle.

---

## 5. Adjacent method, not a volume method

> Jung, J., & Yun, S.-H. (2020). Evaluation of coherent and incoherent
> landslide detection methods based on synthetic aperture radar for rapid
> response. **Remote Sensing 12**(2), 265.
> doi:[10.3390/rs12020265](https://doi.org/10.3390/rs12020265) (open access)

Backs the SAR tab's amplitude log-ratio change detection
(`sar_change.py`). It finds *where* the slide is; it does not size it. Listed
so the plugin's reference set is complete.

---

## 6. Discrepancies found while compiling this

Three small things in existing files, none of which change a computed number:

1. **`deposit_thickness_card.html:218`** attributes Bessette-Kirton et al.
   (2018) to *Landslides*. It is **JGR Earth Surface 123(4), 622–641**,
   doi:10.1002/2017JF004512.
2. **`deposit_thickness_card.html:214`** gives Toney et al. as pages 271–**291**.
   It is 271–**293**.
3. **`deposit_thickness_card.html` §3 and its "small event" note overstate the
   evidence** — see the box in §3 above. The card calls the Iliamna 1.5 m
   "directly-measured" alongside Sherman; it is assumed. The same wording is in
   the Volume tab's thickness note, which now belongs to the DEFAULT fit, so it
   has been corrected there.
4. **The default thickness bracket is written three different ways**: the card
   and the Volume tab note say `1.5 m (1–2.5)`, while
   `volume_calc.FIT_OUTLINE["thickness"]` says `~1.5 m (0.5–3 m)`. Toney et
   al.'s own figure is **1.5 ± 1 m**, i.e. **0.5–2.5 m**. Worth picking one.

---

## 7. Has anyone constrained it since? (checked 2026-09-21)

Smith et al. (2023) listed supraglacial area–volume scaling as future work. **No
one has published it.** What has appeared since goes the other way — more
single-event volumes measured from DEMs, which is a reason to prefer §2 where a
DEM pair exists, not a better scaling law:

* **Tracy Arm, SE Alaska, 10 Aug 2025** — >64×10⁶ m³ onto South Sawyer Glacier
  and into the fjord, 481 m runup. A single event, glacier-emplaced, and the
  closest thing to our setting published recently.
  > Shugar, D. H., Barnhart, K. R., Berdahl, M., Caplan-Auerbach, J.,
  > Ekström, G., et al. (2026). A 481 m-high landslide-tsunami in a cruise
  > ship-frequented Alaska fjord. **Science**.
  > doi:[10.1126/science.aec3187](https://doi.org/10.1126/science.aec3187)

* **Jan Mayen, 10 Mar 2025** — an Mʳ 6.5 transform earthquake triggered a rock
  avalanche onto Kjerulf Glacier ~7 km from the epicentre, reconstructed from
  seismic, GNSS, infrasound and imagery. Directly relevant to this plugin's
  premise, and to the epicentre-distance prior.
  > Earthquake-triggered cascading hazards under Arctic amplification (2026).
  > **PNAS**. doi:[10.1073/pnas.2617210123](https://doi.org/10.1073/pnas.2617210123)

* **Not applicable:** Korup, Pánek & Břežný (2025), *Comms Earth & Environ.*,
  doi:[10.1038/s43247-025-02614-5](https://doi.org/10.1038/s43247-025-02614-5)
  models volume for Earth's largest landslides — but its floor is **1 km³**
  (1000 Mm³), three orders of magnitude above our events.

### The 2/3 spreading law — published, calibrated, and wrong for ice

There **is** a properly calibrated area–volume relation for rock avalanches. It
is not the one to use here, and finding out why is the useful part.

> Griswold, J. P., & Iverson, R. M. (2008, rev. 2014). Mobility statistics and
> automated hazard mapping for debris flows and rock avalanches.
> **USGS Scientific Investigations Report 2007–5276**.
> [pubs.usgs.gov/sir/2007/5276](https://pubs.usgs.gov/sir/2007/5276/)

Planimetric inundation area goes as the 2/3 power of volume — a form supported
by Davies (1982), Hungr (1990), Vallance & Scott (1997), Dade & Huppert (1998),
Iverson et al. (1998) and Legros (2002), and here **regressed on 143 rock
avalanches spanning 10⁵–10¹¹ m³**, r² = 0.76–0.91:

```
B = α · V^(2/3)          α = 20 for rock avalanches (also debris flows;
                                 200 for lahars)
inverted for our workflow:   V = (B / α)^(3/2)
```

Note the form is the same power law as Larsen's, seen from the other side:
`V ∝ A^1.5` against Larsen's `V ∝ A^1.41` for bedrock scars. Both say mean
thickness grows with size; neither is constant.

**Applied to supraglacial deposits, α = 20 fails badly:**

| Event | Outline | V measured | V at α = 20 | Error |
|---|---|---|---|---|
| Sherman 1964 | 8.25 km² | 10.1 Mm³ | 265 Mm³ | **26× high** |
| Lamplugh 2016 | 22.19 km² | ~70 Mm³ | 1169 Mm³ | **17× high** |

At 12 Mm³ it implies a mean thickness of 11.4 m where Toney et al. assume 1.5 m.
That is the substrate: α = 20 is calibrated on *subaerial* rock avalanches, and
ice is exactly what changes the spreading.

**Re-anchoring the same form on ice.** Fitting α to Toney's Iliamna figure
(12 Mm³ at 1.5 m) gives **α ≈ 153** — i.e. a supraglacial deposit covers about
**7.6× the area** of a subaerial rock avalanche of the same volume. That is a
number for what Smith et al. and Sosio et al. (2012) describe qualitatively. It
then reproduces the cases it was *not* fitted to:

| Event | Predicted | Observed |
|---|---|---|
| Sherman 1964, volume from 8.25 km² | 12.6 Mm³ | 10.1 Mm³ |
| Lamplugh 2016, volume from 22.19 km² | 55 Mm³ | ~70 Mm³ (incl. entrained ice) |
| Brenndalsbreen 2010, thickness at 0.13 Mm³ | 0.33 m | 0.40 m |

**What is and is not established here.** The functional form is well published
and tested over six orders of magnitude. The coefficient **for ice is not
published** — α ≈ 153 is fitted here to a single point and tested against two
events, which is not a calibration. Griswold & Iverson's own scatter is 0.32–0.45
in log₁₀ (a factor of 2.1–2.8), so even the published version is not precise.
Deriving a supraglacial α properly — from Smith et al.'s 69 mapped areas plus
DEM-differenced volumes — is the paper nobody has written.

**What it would change.** For a 1 km² outline, near Smith et al.'s 1.16 km²
Alaska mean:

| | Volume for a 1 km² outline |
|---|---|
| Constant 1.5 m (what the tab does now) | 1.50 Mm³ |
| Supraglacial α ≈ 153 | **0.53 Mm³** |
| Published α = 20 (subaerial — do not use) | 11.2 Mm³ |

**Not implemented.** This is a science call, not a code call.

---

## Full reference list

| Paper | Backs | Access |
|---|---|---|
| Larsen, Montgomery & Korup 2010, *Nat. Geosci.* 3, 247–251, [10.1038/ngeo776](https://doi.org/10.1038/ngeo776) | `V = αA^γ`, Table S1 scar coefficients | paywalled; [author PDF](https://gis.ess.washington.edu/grg/publications/pdfs/Larsen_et_al_2010.pdf) |
| Nuth & Kääb 2011, *The Cryosphere* 5, 271–290, [10.5194/tc-5-271-2011](https://doi.org/10.5194/tc-5-271-2011) | dDEM vertical co-registration | open access |
| Bessette-Kirton, Coe & Zhou 2018, *JGR Earth Surf.* 123, 622–641, [10.1002/2017JF004512](https://doi.org/10.1002/2017JF004512) | ablation/entrainment/compaction on glaciers | paywalled |
| Toney, Fee, Allstadt, Haney & Matoza 2021, *Earth Surf. Dynam.* 9, 271–293, [10.5194/esurf-9-271-2021](https://doi.org/10.5194/esurf-9-271-2021) | area × thickness; 1.5 ± 1 m | open access |
| Shreve 1966, *Science* 154, 1639–1643, [10.1126/science.154.3757.1639](https://doi.org/10.1126/science.154.3757.1639) | measured 1.65 m supraglacial sheet | paywalled |
| Waythomas, Miller & Beget 2000, *JVGR* 104, 97–130, [10.1016/S0377-0273(00)00202-X](https://doi.org/10.1016/S0377-0273(00)00202-X) | prior Red Glacier deposit thickness | paywalled |
| Huggel et al. 2007, *JVGR* 168, 114–136, [10.1016/j.jvolgeores.2007.08.009](https://doi.org/10.1016/j.jvolgeores.2007.08.009) | Iliamna ice-rock avalanche setting | paywalled |
| Higman et al. 2018, *Sci. Rep.* 8, 12993, [10.1038/s41598-018-30475-w](https://doi.org/10.1038/s41598-018-30475-w) | large-event thickness comparator | open access |
| Ekström & Stark 2013, *Science* 339, 1416–1419, [10.1126/science.1232887](https://doi.org/10.1126/science.1232887) | seismic mass/volume cross-check | paywalled |
| Griswold & Iverson 2008 (rev. 2014), *USGS SIR* 2007–5276, [pubs.usgs.gov/sir/2007/5276](https://pubs.usgs.gov/sir/2007/5276/) | `B = 20·V^(2/3)` for rock avalanches, 143 events — subaerial, fails on ice | open access |
| Smith et al. 2023, *Geomorphology* 425, 108591, [10.1016/j.geomorph.2023.108591](https://doi.org/10.1016/j.geomorph.2023.108591) | 69-event Alaska supraglacial inventory; scaling still an open problem | open access |
| Engen et al. 2024, *Landslides*, [10.1007/s10346-024-02275-z](https://doi.org/10.1007/s10346-024-02275-z) | measured small supraglacial deposit, 0.40 ± 0.20 m | paywalled |
| Jung & Yun 2020, *Remote Sens.* 12, 265, [10.3390/rs12020265](https://doi.org/10.3390/rs12020265) | SAR change detection (not volume) | open access |
