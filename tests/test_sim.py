"""Physics unit tests (SPEC §4): uniform wind, zero wind, boundary-layer cap, mass, gridding."""

import numpy as np
import pytest

from sim.constants import M_PER_DEG
from sim.engine import Grid, Params, Receptor, make_particles, run_backward, run_forward, step
from sim.met import Met

GRID = Grid(70, 26, 80, 34)


def met(u=0.0, v=0.0, hpbl=1000.0, hours=72):
    """Test weather: constant in space and time unless hpbl is a function of the hour."""
    times = np.arange(hours + 1) * 3600.0
    lats, lons = np.arange(25, 35.01, 0.25), np.arange(69, 81.01, 0.25)
    shape = (len(times), 4, len(lats), len(lons))
    h = np.array([hpbl(i) if callable(hpbl) else hpbl for i in range(hours + 1)], dtype=float)
    return Met(
        times=times, lats=lats, lons=lons, u=np.full(shape, u, np.float32),
        v=np.full(shape, v, np.float32),
        hpbl=np.broadcast_to(h[:, None, None], (len(times), len(lats), len(lons))).copy(),
    )  # fmt: skip


def particles(n=500, lon=75.0, lat=30.0, mass=1.0):
    return make_particles(np.full(n, lon), np.full(n, lat), np.full(n, mass), np.zeros(n),
                          np.zeros(n, dtype=int))  # fmt: skip


def advance(m, p, hours, prm):
    rng = np.random.default_rng(1)
    for i in range(int(hours * 3600 / prm.dt)):
        step(m, p, np.flatnonzero(p.alive), i * prm.dt, prm.dt, prm, rng)


def test_zero_wind_particles_stay_put():
    p = particles()
    advance(met(), p, 24, Params(k_h=0, v_dep=0))
    assert np.allclose(p.lon, 75.0) and np.allclose(p.lat, 30.0)
    assert np.isclose(p.mass.sum(), 500.0) and p.alive.all()


def test_uniform_wind_moves_particles_the_right_distance():
    p = particles()
    advance(met(u=5.0, v=-2.0), p, 6, Params(k_h=0, v_dep=0))
    east_m = (p.lon - 75.0) * M_PER_DEG * np.cos(np.radians(30.0))
    north_m = (p.lat - 30.0) * M_PER_DEG
    assert np.allclose(north_m, -2.0 * 6 * 3600, rtol=1e-6)
    assert np.allclose(east_m, 5.0 * 6 * 3600, rtol=5e-3)  # cos(lat) changes a little on the way


def test_backward_step_undoes_forward_step():
    p, prm, m = particles(), Params(k_h=0, v_dep=0), met(u=4.0, v=3.0)
    rng = np.random.default_rng(1)
    step(m, p, np.arange(p.n), 0.0, 600.0, prm, rng)
    step(m, p, np.arange(p.n), 600.0, -600.0, prm, rng)
    assert np.allclose(p.lon, 75.0, atol=1e-5) and np.allclose(p.lat, 30.0, atol=1e-9)


def test_boundary_layer_cap():
    p = particles()
    advance(met(hpbl=800.0), p, 12, Params(v_dep=0))
    assert p.z.max() <= 800.0 and p.z.min() >= 0.0
    assert p.z.std() > 150  # spread through the layer, not stuck at one height


def test_particles_above_a_collapsed_layer_stay_aloft_then_mix_down_again():
    def h(hour):
        return 1500.0 if hour < 3 or hour >= 9 else 200.0

    m, p, prm = met(hpbl=h), particles(2000), Params(k_h=0, v_dep=0)
    rng = np.random.default_rng(1)
    z_night = None
    for i in range(12 * 6):
        step(m, p, np.arange(p.n), i * 600.0, 600.0, prm, rng)
        if i == 6 * 6:  # hour 6: layer is 200 m
            z_night = p.z.copy()
    aloft = z_night > 200.0
    assert 0.5 < aloft.mean() < 0.95  # most smoke is left above the shallow night layer
    assert p.z.max() <= 1500.0 and (p.z < 200).mean() < 0.3  # mixed through 1500 m again


def test_deposition_matches_exponential_decay():
    p, prm = particles(), Params(k_h=0, v_dep=0.002)
    advance(met(hpbl=500.0), p, 24, prm)
    assert np.allclose(p.mass, np.exp(-0.002 * 24 * 3600 / 500.0), rtol=1e-9)


def test_particles_leaving_the_grid_are_dropped():
    p = particles()
    advance(met(u=30.0), p, 72, Params(k_h=0, v_dep=0))
    assert not p.alive.any()


def test_ground_concentration_equals_mass_over_volume():
    m, p = met(hpbl=1000.0, hours=2), particles(n=1000, lon=75.05, lat=30.05, mass=2.0)
    rec = Receptor("x", 30.05, 75.05)
    times, conc, contrib, stats = run_forward(m, p, GRID, [rec], 1, 0.0, prm=Params(k_h=0, v_dep=0))
    area = (0.1 * M_PER_DEG) ** 2 * np.cos(np.radians(30.05))
    expected = 2000.0 * 1e9 / (area * 1000.0)  # µg/m³
    assert conc.shape == (1, 80, 100)
    assert conc[0].max() == pytest.approx(expected, rel=1e-4)
    assert np.count_nonzero(conc[0]) == 1
    assert contrib[0, 0, 0] == pytest.approx(2000.0 * 1e9 / (rec.area_m2() * 1000.0), rel=1e-4)


def test_contribution_goes_to_the_downwind_receptor_only():
    # 5 m/s eastward wind: smoke released at 75E reaches a receptor at 76E (about 96 km) in ~5 h.
    m, p = met(u=5.0, hours=12), particles(n=2000)
    recs = [Receptor("downwind", 30.0, 76.0), Receptor("upwind", 30.0, 74.0)]
    times, conc, contrib, _ = run_forward(m, p, GRID, recs, 1, 12 * 3600.0, prm=Params(v_dep=0))
    assert contrib[:, 1, 0].max() == 0.0
    peak_hour = times[contrib[:, 0, 0].argmax()] / 3600
    assert 4 <= peak_hour <= 7


def test_backward_footprint_points_upwind():
    m = met(u=5.0, hours=24)
    foot = run_backward(m, [Receptor("x", 30.0, 76.0)], GRID, np.array([24 * 3600.0]), 0.0)
    lons = GRID.lons
    west, east = foot[0][:, lons < 75.7].sum(), foot[0][:, lons > 76.3].sum()
    assert west > 0 and east == 0.0


def test_emission_arithmetic_and_particle_mass():
    from sim import constants as c
    from sim.fires import Fires, emission_area_kg, emission_frp_kg, particles_for

    # Method B: 10 MW for one hour = 36,000 MJ -> 13,248 kg dry matter -> x EF g/kg
    assert emission_frp_kg(10.0, 6.26) == pytest.approx(36000 * 0.368 * 6.26 / 1000)
    # Method A: 2 pixels x 0.14 km2 x 0.902 kg/m2 = 252,560 kg dry matter -> x EF g/kg
    assert emission_area_kg(2, 13.4) == pytest.approx(2 * 140_000 * 0.902 * 13.4 / 1000)
    t0 = np.datetime64("2026-11-14T12:00:00", "s")
    f = Fires(
        cluster=np.array([0, 0, 1]), lon=np.array([75.0, 75.01, 76.0]), lat=np.full(3, 30.0),
        observed=t0 + np.array([-3600, 0, 7200]).astype("timedelta64[s]"),
        frp=np.array([1.0, 3.0, 20.0]), pixels=np.array([1.0, 1.0, 4.0]),
    )  # fmt: skip
    lon, lat, mass, release, cluster, mass2 = particles_for(f, t0, 1000)
    assert mass.sum() == pytest.approx(emission_area_kg(6))
    assert mass[cluster == 1].sum() == pytest.approx(emission_area_kg(4))
    assert mass2.sum() == pytest.approx(emission_frp_kg(24.0))
    assert release.min() >= -3600 and release.max() <= 7200 + c.BURN_SECONDS


def test_level_heights_follow_the_terrain():
    """With true level heights, a particle at 500 m sits between the 10 m wind and the first
    pressure level wherever that level really is, not at a fixed 762 m."""
    m = met(hours=2)
    m.u[:, 0], m.u[:, 1], m.u[:, 2], m.u[:, 3] = 0.0, 10.0, 20.0, 30.0
    shape = (len(m.times), 3, len(m.lats), len(m.lons))
    z = np.empty(shape, dtype=np.float32)
    z[:, 0], z[:, 1], z[:, 2] = 510.0, 1300.0, 2900.0
    low = Met(times=m.times, lats=m.lats, lons=m.lons, u=m.u, v=m.v, hpbl=m.hpbl, z_agl=z)
    pos = (np.array([75.1]), np.array([30.1]))
    assert m.wind(0.0, *pos, np.array([510.0]))[0][0] == pytest.approx(10 * 500 / 752, rel=1e-4)
    assert low.wind(0.0, *pos, np.array([510.0]))[0][0] == pytest.approx(10.0, rel=1e-4)
    assert low.wind(0.0, *pos, np.array([905.0]))[0][0] == pytest.approx(15.0, rel=1e-4)
    assert low.wind(0.0, *pos, np.array([9000.0]))[0][0] == pytest.approx(30.0, rel=1e-4)
    assert low.wind(0.0, *pos, np.array([0.0]))[0][0] == pytest.approx(0.0, abs=1e-6)


def test_met_uses_newest_cycle_and_older_one_only_before_it(tmp_path):
    import xarray as xr

    from sim.met import from_netcdf

    def cycle(start, wind):
        t = np.datetime64(start) + np.arange(13) * np.timedelta64(1, "h")
        lat, lon, lev = (
            np.array([31.0, 30.0]),
            np.array([75.0, 76.0]),
            np.array([925.0, 850.0, 700.0]),
        )
        s3, s2 = (13, 3, 2, 2), (13, 2, 2)
        d3 = ("valid_time", "isobaricInhPa", "latitude", "longitude")
        d2 = ("valid_time", "latitude", "longitude")
        ds = xr.Dataset(
            {"u": (d3, np.full(s3, wind)), "v": (d3, np.zeros(s3)), "u10": (d2, np.full(s2, wind)),
             "v10": (d2, np.zeros(s2)), "hpbl": (d2, np.full(s2, 500.0)),
             "t2m": (d2, np.full(s2, 290.0))},
            coords={"valid_time": t, "latitude": lat, "longitude": lon, "isobaricInhPa": lev},
        )  # fmt: skip
        path = tmp_path / f"{wind}.nc"
        ds.to_netcdf(path)
        return path

    old, new = cycle("2026-11-14T06:00", 1.0), cycle("2026-11-14T12:00", 9.0)
    m = from_netcdf([old, new])
    assert m.times[0] == -6 * 3600 and m.times[-1] == 12 * 3600 and len(m.times) == 19
    pos = (np.array([75.5]), np.array([30.5]), np.array([100.0]))
    assert m.wind(-3 * 3600.0, *pos)[0][0] == pytest.approx(1.0)  # before the new cycle: old one
    assert m.wind(3 * 3600.0, *pos)[0][0] == pytest.approx(9.0)  # after: new one, not the old
    assert list(m.lats) == [30.0, 31.0]


def test_forward_and_backward_runs_agree_for_a_steady_source():
    """Reciprocity: a steady source seen from the receptor must give the same concentration
    whether we follow the smoke forward or the air backward."""
    m = met(u=5.0, hpbl=1000.0, hours=48)
    rng = np.random.default_rng(3)
    n, q_kg_s, span = 40_000, 1.0, 48 * 3600.0
    p = make_particles(
        75.0 + rng.random(n) * 0.1, 30.0 + rng.random(n) * 0.1, np.full(n, q_kg_s * span / n),
        rng.random(n) * span, np.zeros(n, dtype=int),
    )  # fmt: skip
    rec = Receptor("x", 30.05, 76.05)
    prm = Params(v_dep=0)
    times, _, contrib, _ = run_forward(m, p, GRID, [rec], 1, span, prm=prm)
    late = times >= 36 * 3600
    forward = contrib[late, 0, 0].mean()
    foot = run_backward(m, [rec], GRID, times[late], 0.0, n_per_arrival=3000, prm=prm)
    iy, ix = int((30.05 - 26) / 0.1), int((75.05 - 70) / 0.1)
    backward = foot[0, iy, ix] * q_kg_s * 1e9
    assert forward > 0 and backward == pytest.approx(forward, rel=0.15)


def test_same_seed_gives_identical_results():
    def once():
        m, p = met(u=3.0, v=1.0, hours=12), particles(n=3000)
        return run_forward(m, p, GRID, [Receptor("x", 30.3, 76.0)], 1, 12 * 3600.0)[1]

    assert np.array_equal(once(), once())


def test_backward_contributions_match_forward_for_a_short_fire():
    """A one-hour fire: smoke per receptor-hour from the backward run must match the forward
    run in total and in timing, and must be attributed to the right cluster."""
    from sim.engine import Sources

    m = met(u=5.0, hpbl=1000.0, hours=24)
    rng = np.random.default_rng(5)
    n, kg = 40_000, 3600.0
    p = make_particles(
        75.0 + rng.random(n) * 0.1, 30.0 + rng.random(n) * 0.1, np.full(n, kg / n),
        2 * 3600.0 + rng.random(n) * 3600.0, np.ones(n, dtype=int), mass2=np.full(n, 2 * kg / n),
    )  # fmt: skip
    rec = [Receptor("x", 30.05, 76.05), Receptor("upwind", 30.05, 74.0)]
    prm = Params(v_dep=0.002)
    times, _, fwd, stats = run_forward(m, p, GRID, rec, 2, 24 * 3600.0, prm=prm)
    src = Sources(
        lon=np.array([75.05]), lat=np.array([30.05]), start=np.array([2 * 3600.0]),
        group=np.array([1]), rate=np.array([[kg / 3600.0], [2 * kg / 3600.0]]), duration=3600.0,
    )  # fmt: skip
    foot, back = run_backward(m, rec, GRID, times, 0.0, 1500, prm, sources=src, n_groups=2)
    assert back.shape == (2, len(times), 2, 2)
    assert back[:, :, 1].sum() == 0 and back[:, :, :, 0].sum() == 0  # upwind / other cluster
    f, b = fwd[:, 0, 1], back[0, :, 0, 1]
    assert b.sum() == pytest.approx(f.sum(), rel=0.15)
    assert abs(int(b.argmax()) - int(f.argmax())) <= 1
    assert back[1].sum() == pytest.approx(2 * back[0].sum(), rel=1e-5)  # second method = 2x
    assert stats["contrib2"].sum() == pytest.approx(2 * fwd.sum(), rel=1e-5)


def test_forward_and_backward_agree_in_spreading_wind():
    """Wind that speeds up downwind (divergent flow) thins the smoke. The backward run must
    account for that, or it overstates what reaches the receptor."""
    from sim.engine import Sources

    m = met(hpbl=1000.0, hours=24)
    x_m = (m.lons - 74.0) * M_PER_DEG * np.cos(np.radians(30.0))
    m.u[:] = (3.0 + 4e-5 * x_m)[None, None, None, :]  # 3 m/s at 74E, about 11 m/s at 76E
    m = Met(times=m.times, lats=m.lats, lons=m.lons, u=m.u, v=m.v, hpbl=m.hpbl)
    rng = np.random.default_rng(7)
    n, kg = 40_000, 3600.0
    p = make_particles(
        75.0 + rng.random(n) * 0.1, 30.0 + rng.random(n) * 0.1, np.full(n, kg / n),
        3600.0 + rng.random(n) * 3600.0, np.zeros(n, dtype=int),
    )  # fmt: skip
    rec = [Receptor("x", 30.05, 77.05)]
    prm = Params(v_dep=0)
    times, _, fwd, _ = run_forward(m, p, GRID, rec, 1, 24 * 3600.0, prm=prm)
    src = Sources(
        lon=np.array([75.05]), lat=np.array([30.05]), start=np.array([3600.0]),
        group=np.array([0]), rate=np.array([[kg / 3600.0]]), duration=3600.0,
    )  # fmt: skip
    _, back = run_backward(m, rec, GRID, times, 0.0, 1500, prm, sources=src, n_groups=1)
    assert fwd.sum() > 0 and back.sum() == pytest.approx(fwd.sum(), rel=0.15)
