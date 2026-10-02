from radarforge.services import guide


def test_reflectivity_bands():
    assert "light" in guide.hint("REF", 15)
    assert "hail" in guide.hint("REF", 62)


def test_correlation_coefficient():
    assert "uniform" in guide.hint("CC", 0.99)
    assert "debris" in guide.hint("CC", 0.7)


def test_velocity_direction_and_units():
    assert "toward" in guide.hint("VEL", -20)       # m/s, negative is inbound
    assert "away" in guide.hint("SRV", 15)
    assert "40 kt" not in guide.hint("VEL", 10)      # 10 m/s is ~19 kt


def test_range_fold_and_nan():
    assert "range folded" in guide.hint("VEL", float("inf"))
    assert guide.hint("REF", float("nan")) is None
    assert guide.hint("REF", None) is None
    assert guide.hint("NOPE", 3) is None


def test_azimuthal_shear_sign():
    assert "cyclonic" in guide.hint("AZSH", 15) and "anticyclonic" not in guide.hint("AZSH", 15)
    assert "anticyclonic" in guide.hint("AZSH", -15)


def test_hail_core_needs_all_three():
    assert "hail core" in guide.combined({"REF": 60.0, "CC": 0.9, "ZDR": 0.3})
    assert guide.combined({"REF": 60.0, "CC": 0.99, "ZDR": 0.3}) is None
    assert guide.combined({"REF": 60.0}) is None
