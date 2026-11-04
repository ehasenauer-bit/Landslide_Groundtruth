"""PARKED 2026-11-04 — the Volume tab's deposit area × thickness fit.

Removed from qgis_plugin/landslide_groundtruth/volume_tab.py, verbatim, when
Larsen et al. (2010) became the tab's one volume. Not imported or packaged by
anything; see README.md beside this file for why and how to put it back.

Methods are exactly as they stood in the tab at commit cae8034. The few pieces
that were inline inside methods that stayed (_build_volumes_box, _measure,
_show_current) are reproduced as the marked snippets at the bottom, unchanged
except for the def line wrapped round them.
"""
from qgis.PyQt.QtCore import QMetaType, Qt
from qgis.PyQt.QtWidgets import (
    QWidget, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QComboBox,
)
from qgis.gui import QgsCollapsibleGroupBox

from . import volume_calc_thickness as volume_calc   # parked THICKNESS_SETTINGS etc.
from .volume_tab import _fmt, _km2, _vol_range


# --- the rows each table carried for this fit (volume_tab, cae8034) ---------
WRITEBACK_FIELDS_THICKNESS = [
    ("thick_m", QMetaType.Type.Double, "t_best"),
    ("thick_lo_m", QMetaType.Type.Double, "t_low"),
    ("thick_hi_m", QMetaType.Type.Double, "t_high"),
    ("vthk_m3", QMetaType.Type.Double, "v_thick"),
    ("vthk_lo_m3", QMetaType.Type.Double, "v_thick_lo"),
    ("vthk_hi_m3", QMetaType.Type.Double, "v_thick_hi"),
]
TABLE_COLS_THICKNESS = ["Thickness (m)", "V thick (m³)", "V thick low", "V thick high"]
CSV_FIELDS_THICKNESS = [
    ("thickness_setting", "thick_setting"),
    ("thickness_m", "t_best"), ("thickness_low_m", "t_low"),
    ("thickness_high_m", "t_high"),
    ("thickness_outline", "thick_from"),
    ("thickness_area_m2", "thick_area_m2"),
    ("vol_thickness_m3", "v_thick"), ("vol_thickness_lo_m3", "v_thick_lo"),
    ("vol_thickness_hi_m3", "v_thick_hi"),
    ("thickness_calculator", "thick_calc"),
]
# keys _measure initialised in every row
ROW_KEYS_THICKNESS = {
    "v_thick": None, "v_thick_lo": None, "v_thick_hi": None,
    "thick_area_m2": None, "thick_from": "", "thick_setting": "",
    "t_best": None, "t_low": None, "t_high": None, "thick_calc": "",
}


class ThicknessMixin:
    """The area × thickness methods of VolumeTab, verbatim."""

    # ---- the Settings box as it was, thickness AND material (cae8034) ----
    def _build_settings_box(self):
        """What the two fits need beyond the outlines.

        Deposit thickness for area × thickness (Toney et al. 2021): V = the total
        outline's plan area × mean thickness, the low/high thickness giving the
        range. The mean thickness is the dominant uncertainty, which is why it's
        carried as an explicit low/best/high rather than hidden in a
        coefficient. What failed (bedrock/soil) for Larsen et al. (2010) — read
        by that fit only."""
        box = QgsCollapsibleGroupBox("Settings")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)
        # Underscored so project_state does not store it: the setting is read back
        # off the three numbers (_sync_thickness_setting), which ARE stored.
        self._thick_setting_combo = QComboBox()
        for label, key, *_rest in volume_calc.THICKNESS_SETTINGS:
            self._thick_setting_combo.addItem(label, key)
            # the closed combo elides (below), so the full label rides along
            self._thick_setting_combo.setItemData(
                self._thick_setting_combo.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
        self._thick_setting_combo.addItem("Custom — your own values", "custom")
        self._thick_setting_combo.setToolTip(
            "What kind of deposit the TOTAL outline is, for area × thickness. "
            "Fills the mean thickness and its low/high from the published values "
            "for that setting; edit any of them and this reads Custom.\n\n"
            "Near date / far date is when the imagery you outlined was taken: "
            "soon after the event the deposit still holds the snow and ice it "
            "picked up, as a seismic volume does; after a melt season only the "
            "rock is left.")
        # The labels carry their numbers, so the longest would set the dock's
        # minimum width; let the closed combo elide instead (the list still opens
        # full width).
        self._thick_setting_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._thick_setting_combo.setMinimumContentsLength(12)
        self._thick_setting_combo.activated.connect(self._apply_thickness_setting)
        form.addRow("Deposit thickness", self._thick_setting_combo)

        _label, _key, best, low, high, _note = volume_calc.THICKNESS_SETTINGS[0]
        self.thick_best_edit = QLineEdit(f"{best:g}")
        self.thick_best_edit.setToolTip(
            "Mean deposit thickness in metres, averaged over the whole outline "
            "(thick in the source/deposit, thin along the track).")
        self.thick_low_edit = QLineEdit(f"{low:g}")
        self.thick_low_edit.setToolTip(
            "Low thickness → low volume. Must be less than the mean; blank for no "
            "lower bound.")
        self.thick_high_edit = QLineEdit(f"{high:g}")
        self.thick_high_edit.setToolTip(
            "High thickness → high volume. Must be greater than the mean; blank "
            "for no upper bound.")
        # its own full-width row: sharing the field column with the "Deposit
        # thickness" label squeezed the three boxes until "2.5" read ".5"
        nums_w = QWidget()
        nums = QHBoxLayout(nums_w)
        nums.setContentsMargins(0, 0, 0, 0)
        nums.setSpacing(4)
        nums.addStretch(1)
        for text, edit in (("Mean", self.thick_best_edit),
                           ("low", self.thick_low_edit),
                           ("high", self.thick_high_edit)):
            edit.setMinimumWidth(40)
            edit.setMaximumWidth(64)
            nums.addWidget(QLabel(text))
            nums.addWidget(edit)
        nums.addWidget(QLabel("m"))
        form.addRow(nums_w)
        self._thick_note_lbl = self._help()
        form.addRow(self._thick_note_lbl)     # spans both columns: it is prose
        form.addRow(self._help(
            "These are means over the deposit. The total outline also holds the "
            "source scar and track — Bessette-Kirton & Coe (2020) take the "
            "deposit as ~80% of it — so the volume reads somewhat high; the low "
            "end covers that."))

        self.material_combo = QComboBox()
        for label, _key in volume_calc.MATERIALS:
            self.material_combo.addItem(label)
        self.material_combo.setToolTip(
            "Read by Larsen et al. (2010) only. Bedrock failures are deeper for a "
            "given area than soil failures. This is the single biggest control on "
            "that answer: at a typical scar size the two options differ by about "
            "a factor of 5 — roughly twenty times the ±range shown beside the "
            "volume. Pick it from what actually failed, not from the surrounding "
            "cover.")
        form.addRow("What failed?", self.material_combo)
        # The case this plugin exists for is the one Larsen does not cover.
        form.addRow(self._help(
            "For Larsen et al. (2010). Neither option covers a "
            "rock-and-ice avalanche: if much of what moved was ice or entrained "
            "snow, that relation is outside its calibration — and the seismic "
            "volume is a mass divided by an assumed density, so check both "
            "numbers assume the same thing before comparing them."))

        for edit in (self.thick_best_edit, self.thick_low_edit,
                     self.thick_high_edit):
            edit.textChanged.connect(self._sync_thickness_setting)
        self._sync_thickness_setting()
        return box

    def _apply_thickness_setting(self, index):
        """Fill mean/low/high from the setting picked at `index`.

        Wired to the combo's `activated` — a user's pick only — so the
        programmatic moves made by _sync_thickness_setting never refill the
        numbers. Custom leaves them for the analyst to type."""
        key = self._thick_setting_combo.itemData(index)
        entry = next((s for s in volume_calc.THICKNESS_SETTINGS if s[1] == key),
                     None)
        if entry is None:
            self._show_thickness_note()
            self.thick_best_edit.setFocus()
            return
        _label, _key, best, low, high, _note = entry
        for edit, val in ((self.thick_best_edit, best), (self.thick_low_edit, low),
                          (self.thick_high_edit, high)):
            edit.setText(f"{val:g}")
        # setText only signals a CHANGE, so sync once more for a pick whose
        # numbers were already in the fields (Custom, then the same setting).
        self._sync_thickness_setting()

    def _sync_thickness_setting(self, *_args):
        """Point the Setting combo at whichever setting the three numbers are, or
        Custom when they are none of them. Runs on every edit and on project
        restore, which is why the combo itself never needs storing."""
        entry = volume_calc.thickness_setting(*self._thickness_values())
        combo = self._thick_setting_combo
        combo.setCurrentIndex(combo.findData(entry[1] if entry else "custom"))
        self._show_thickness_note()

    def _show_thickness_note(self):
        """The selected setting's evidence, under the combo."""
        key = self._thick_setting_combo.currentData()
        entry = next((s for s in volume_calc.THICKNESS_SETTINGS if s[1] == key),
                     None)
        self._thick_note_lbl.setText(
            entry[5] if entry else
            "Your own mean/low/high — none of the settings above. Pick a setting "
            "to fill them from the published values again.")

    def _thickness_values(self):
        """(t_best, t_low, t_high) in metres from the thickness fields, or
        (None, None, None) if the mean is blank/invalid. Low/high come back None
        unless they parse to a positive number."""
        def _f(edit):
            try:
                val = float(edit.text().strip())
                return val if val > 0 else None
            except (ValueError, AttributeError):
                return None
        return (_f(self.thick_best_edit), _f(self.thick_low_edit),
                _f(self.thick_high_edit))

    def _thickness_volume(self, a_total, a_best, warnings):
        """{v_thick…} from area × thickness, or {} with the reason in `warnings`.

        The TOTAL outline is the deposit footprint the published thicknesses
        are means over; with no total drawn the source scar stands in, which
        only makes sense with the Source-scar (detachment depth) setting."""
        t, t_lo, t_hi = self._thickness_values()
        if t is None:
            warnings.append(
                "Area × thickness skipped: enter a mean deposit thickness (m) "
                "under Settings, or pick a setting there.")
            return {}
        if a_total is not None:
            area, role = a_total, "total"
        elif a_best is not None:
            area, role = a_best, "source"
        else:
            return {}
        v, v_lo, v_hi, calc = volume_calc.volume_thickness(
            area, t, t_low=t_lo, t_high=t_hi)
        setting = volume_calc.thickness_setting(t, t_lo, t_hi)
        key = setting[1] if setting else "custom"
        if v_lo is None or v_hi is None:
            warnings.append(
                "Single thickness given, so area × thickness has no range — set "
                "a low and high thickness to bracket it.")
        if key == "ice_rich_far":
            warnings.append(
                "Far-date ice-rich setting: this is the rock left after the ice "
                "melted. Over half of what failed at Red Glacier was ice, so the "
                "failed volume can be 2–3× this — use the near-date setting to "
                "compare with a seismic volume.")
        if key == "scar" and role == "total":
            warnings.append(
                "The Source-scar thickness setting is a detachment depth, but "
                "area × thickness converts the TOTAL outline — that reads several "
                "times too high. Pick a deposit setting.")
        elif key not in ("scar", "custom") and role == "source":
            warnings.append(
                "No total outline, so area × thickness converted the SOURCE scar "
                "with a deposit thickness — that reads low. Draw the total "
                "outline, or pick the Source-scar setting.")
        return {"v_thick": v, "v_thick_lo": v_lo, "v_thick_hi": v_hi,
                "thick_area_m2": area, "thick_from": role,
                "thick_setting": setting[0] if setting else "custom",
                "t_best": t, "t_low": t_lo, "t_high": t_hi, "thick_calc": calc}

    # ---- snippets that were inline in methods the tab kept ----
    def _build_thickness_readout(self, f):
        """From _build_volumes_box: the first row of the Volumes form."""
        self.vol_thick_out = self._ro("press Measure")
        self.vol_thick_out.setToolTip(
            "Total outline's plan area × the mean deposit thickness, with the "
            "low/high thickness as its range — the dominant uncertainty in an "
            "area × thickness estimate. Exact m³ are in the results table, the "
            "CSV and the written attributes.")
        f.addRow("Area × thickness", self.vol_thick_out)

    def _log_thickness(self, row):
        """From _measure, after _record."""
        thick = True
        if thick:
            src = "Total" if row["thick_from"] == "total" else "Source best"
            self._append_log(
                f"Area × thickness: {src} {_fmt(row['thick_area_m2'])} m² × "
                f"{row['t_best']:g} m = {_fmt(row['v_thick'])} m³ "
                f"({_fmt(row['v_thick_lo'])} – {_fmt(row['v_thick_hi'])}); "
                f"{row['thick_calc']}.")

    def _show_thickness(self, c):
        """From _show_current, the first branch under `else:`."""
        if True:
            if c.get("v_thick") is not None:
                where = "" if c.get("thick_from") == "total" else " (source)"
                self.vol_thick_out.setText(
                    f"{_vol_range(c['v_thick'], c['v_thick_lo'], c['v_thick_hi'])}"
                    f"   ·  {_km2(c['thick_area_m2'])}{where} × {c['t_best']:g} m")
            else:
                self.vol_thick_out.setText("— not run (see the note below)")
