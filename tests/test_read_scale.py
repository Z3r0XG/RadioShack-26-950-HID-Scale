"""Unit tests for read_scale.py. No scale required; the logic under test is pure."""

import pytest

import read_scale as rs

# ---------------------------------------------------------------------------
# decode_raw_weight
# ---------------------------------------------------------------------------


def test_decode_big_endian_at_default_offset():
    report = [0] * 6 + [0x12, 0x34]
    assert rs.decode_raw_weight(report) == 0x1234


def test_decode_little_endian_at_default_offset():
    report = [0] * 6 + [0x34, 0x12]
    assert rs.decode_raw_weight(report, endianness="little") == 0x1234


def test_decode_honours_custom_offset():
    report = [0, 0, 0, 0, 0xAB, 0xCD, 0, 0]
    assert rs.decode_raw_weight(report, offset=4) == 0xABCD


def test_decode_full_range():
    assert rs.decode_raw_weight([0] * 6 + [0x00, 0x00]) == 0
    assert rs.decode_raw_weight([0] * 6 + [0xFF, 0xFF]) == 65535


def test_decode_rejects_short_report():
    with pytest.raises(rs.ShortReportError):
        rs.decode_raw_weight([0] * 7)


def test_decode_short_report_message_is_actionable():
    with pytest.raises(rs.ShortReportError, match="need at least 8"):
        rs.decode_raw_weight([0] * 5)


def test_decode_rejects_unknown_endianness():
    with pytest.raises(ValueError):
        rs.decode_raw_weight([0] * 8, endianness="middle")


# ---------------------------------------------------------------------------
# calibrated_ounces
# ---------------------------------------------------------------------------


def test_calibration_subtracts_zero_offset():
    assert rs.calibrated_ounces(1000, 1000, 0.01286) == 0


def test_calibration_handles_negative_delta():
    assert rs.calibrated_ounces(900, 1000, 0.01) == pytest.approx(-1.0)


def test_calibration_scales_linearly():
    assert rs.calibrated_ounces(2000, 1000, 0.01286) == pytest.approx(12.86)


# ---------------------------------------------------------------------------
# format_weight_display
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ounces, expected",
    [
        # Values that round up across a pound boundary.
        (31.999, "2 lb 0.00 oz"),
        (15.999, "1 lb 0.00 oz"),
        (47.999, "3 lb 0.00 oz"),
        # A negative smaller than the displayed precision keeps its sign.
        (-0.009, "-0.01 oz"),
        # Ordinary values.
        (0.0, "0.00 oz"),
        (1.5, "1.50 oz"),
        (15.99, "15.99 oz"),
        (16.0, "1 lb 0.00 oz"),
        (17.25, "1 lb 1.25 oz"),
        (160.0, "10 lb 0.00 oz"),
        (-5.25, "-5.25 oz"),
        (-17.25, "-1 lb 1.25 oz"),
        # Rounds to zero: no negative sign on a displayed zero.
        (-0.004, "0.00 oz"),
    ],
)
def test_imperial_formatting(ounces, expected):
    assert rs.format_weight_display(ounces, is_metric=False) == expected


@pytest.mark.parametrize(
    "ounces, expected",
    [
        # -0.005 oz is -0.14 g: the sign follows the displayed unit.
        (-0.005, "-0.1 g"),
        (0.0, "0.0 g"),
        (1.0, "28.3 g"),
        (-1.0, "-28.3 g"),
        (35.274, "1.000 kg"),
        (-35.274, "-1.000 kg"),
        # Rounds to zero: no "-0.0 g".
        (-0.001, "0.0 g"),
    ],
)
def test_metric_formatting(ounces, expected):
    assert rs.format_weight_display(ounces, is_metric=True) == expected


def test_metric_switches_to_kilograms_consistently():
    """A value that rounds up to 1000 g must display as kg, not '1000.0 g'."""
    just_under = 999.96 / rs.GRAMS_PER_OUNCE
    assert rs.format_weight_display(just_under, is_metric=True) == "1.000 kg"


def test_imperial_never_reports_sixteen_ounces():
    """
    No reading may render an ounces component of 16 or more, in either the
    "N lb M oz" form or the bare-ounces form.
    """
    for step in range(0, 16001):
        ounces = step * 0.01
        result = rs.format_weight_display(ounces, is_metric=False)
        if " lb " in result:
            remainder = float(result.split(" lb ")[1].split(" oz")[0])
        else:
            remainder = float(result.split(" oz")[0])
        assert remainder < rs.OUNCES_PER_POUND, f"{ounces} -> {result}"


def test_no_negative_zero_in_any_unit():
    """
    A value too small to show in the displayed unit prints as a plain zero.
    The thresholds differ per unit: a gram is finer than the two-decimal
    ounce display, so -0.004 oz still shows as -0.1 g in metric.
    """
    for ounces in (-0.0001, -0.001, -0.004):
        assert not rs.format_weight_display(ounces, is_metric=False).startswith("-")
    for ounces in (-0.0001, -0.001):
        assert not rs.format_weight_display(ounces, is_metric=True).startswith("-")


# ---------------------------------------------------------------------------
# check_range
# ---------------------------------------------------------------------------


def test_plausible_weight_passes_range_check():
    config = rs.Config()
    full_scale = int(config.capacity_oz / config.calibration)
    assert rs.check_range(full_scale, config) is None


def test_implausible_weight_is_flagged():
    config = rs.Config()
    assert rs.check_range(20000, config) is not None


def test_range_warning_names_the_capacity_exceeded():
    """The reader has to say which limit was passed for the warning to help."""
    config = rs.Config()
    assert "10 lb" in rs.check_range(20000, config)


def test_range_check_flags_large_negative_readings_too():
    """Over-range is a magnitude question; the sign does not make it valid."""
    config = rs.Config()
    assert rs.check_range(-20000, config) is not None


def test_capacity_is_configurable():
    small = rs.Config(capacity_oz=16.0)
    assert rs.check_range(5000, small) is not None
    assert rs.check_range(1000, small) is None


# ---------------------------------------------------------------------------
# Interface selection
# ---------------------------------------------------------------------------


def test_uses_the_single_interface_the_scale_exposes():
    assert rs.select_interface([{"path": b"only", "usage_page": 0xFF00}])["path"] == b"only"


def test_uses_the_first_interface_when_a_variant_exposes_several():
    devices = [{"path": b"first", "usage_page": 0xFF00}, {"path": b"second", "usage_page": 0x8D}]
    assert rs.select_interface(devices)["path"] == b"first"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_defaults():
    args = rs.build_parser().parse_args([])
    assert args.weight_offset == rs.DEFAULT_WEIGHT_OFFSET
    assert args.endian == rs.DEFAULT_ENDIANNESS
    assert args.metric is False


def test_hex_and_decimal_ids_both_parse():
    args = rs.build_parser().parse_args(["--vid", "0x2233", "--pid", "25379"])
    assert args.vid == 0x2233
    assert args.pid == 25379


def test_protocol_overrides_reach_the_config():
    args = rs.build_parser().parse_args(
        ["--weight-offset", "4", "--endian", "little", "--calibration", "0.0128"]
    )
    config = rs.Config(
        weight_offset=args.weight_offset,
        endianness=args.endian,
        calibration=args.calibration,
    )
    assert config.weight_offset == 4
    assert config.endianness == "little"
    assert config.calibration == 0.0128


# ---------------------------------------------------------------------------
# signed_delta
#
# Fixtures come from a unit whose empty platter reads 56527, close enough to
# the 16-bit ceiling that its rated range crosses the rollover.
# ---------------------------------------------------------------------------

HW_ZERO = 56527


def test_empty_platter_is_zero():
    assert rs.signed_delta(HW_ZERO, HW_ZERO) == 0


@pytest.mark.parametrize(
    "raw, expected_delta, description",
    [
        (57980, 1453, "light load"),
        (65318, 8791, "just below the rollover"),
        (646, 9655, "past the rollover"),
    ],
)
def test_measured_hardware_readings(raw, expected_delta, description):
    assert rs.signed_delta(raw, HW_ZERO) == expected_delta, description


def test_reading_past_the_rollover_is_not_negative():
    """A raw value below the zero offset is a heavy load, not a negative one."""
    assert rs.signed_delta(646, HW_ZERO) == 9655
    assert rs.calibrated_ounces(646, HW_ZERO, rs.DEFAULT_CALIBRATION) > 0


def test_readings_below_zero_stay_negative():
    """A genuine under-zero reading must not be mistaken for a wrap."""
    assert rs.signed_delta(HW_ZERO - 500, HW_ZERO) == -500


def test_delta_is_symmetric_around_the_wrap_boundary():
    for offset in (1, 100, 5000, 32767):
        assert rs.signed_delta((HW_ZERO + offset) & 0xFFFF, HW_ZERO) == offset
        assert rs.signed_delta((HW_ZERO - offset) & 0xFFFF, HW_ZERO) == -offset


def test_full_capacity_resolves_correctly_despite_wrapping():
    """The full rated range must resolve even though it crosses the rollover."""
    full_scale = int(rs.DEFAULT_CAPACITY_OZ / rs.DEFAULT_CALIBRATION)
    raw = (HW_ZERO + full_scale) & 0xFFFF
    assert raw < HW_ZERO, "this test is meaningless unless the value wrapped"
    assert rs.signed_delta(raw, HW_ZERO) == full_scale


# ---------------------------------------------------------------------------
# Rollover behaviour of the aggregates
# ---------------------------------------------------------------------------


def test_center_of_readings_spanning_the_rollover():
    """The zero of a platter jittering across the wrap is on the wrap."""
    center, spread = rs.wrap_aware_center([65535, 0, 65534, 1, 0])
    assert spread <= 3, "jitter of a few counts must not read as full-scale motion"
    assert rs.signed_delta(center, 65535) in range(-3, 4)


def test_center_of_ordinary_readings_is_the_median():
    center, spread = rs.wrap_aware_center([56527, 56528, 56529])
    assert center == 56528
    assert spread == 2


# ---------------------------------------------------------------------------
# Numeric option validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, why",
    [
        (["--calibration", "0"], "zero ounces per count divides by zero"),
        (["--calibration", "-0.01"], "a negative scale factor inverts every reading"),
        (["--weight-offset", "-1"], "negative indexing reads the wrong bytes silently"),
        (["--capacity", "0"], "capacity is a divisor of the range check"),
        (["--capacity", "-5"], "a negative capacity warns about every reading"),
        (["--vid", "0x10000"], "outside the 16-bit USB id range"),
        (["--pid", "-1"], "outside the 16-bit USB id range"),
    ],
)
def test_out_of_range_options_are_rejected(argv, why):
    with pytest.raises(SystemExit):
        rs.build_parser().parse_args(argv)


def test_valid_edges_are_still_accepted():
    args = rs.build_parser().parse_args(
        ["--weight-offset", "0", "--vid", "0xFFFF", "--pid", "0", "--calibration", "0.0001"]
    )
    assert args.weight_offset == 0
    assert args.vid == 0xFFFF
    assert args.pid == 0
