#!/usr/bin/env python3
"""Reproducible physics audit; does not modify renderer code or production assets.

Run: venv/bin/python tools/physics_audit.py --output /tmp/physics_audit.json
Exit 1 means a correctness check failed. Numerical tolerances are audit criteria,
not a statement of the application's advertised accuracy. Temporary fixtures are
deleted automatically. Reference orbit/attitude calculations are independent of
yoshimulib's orbital conversion and quaternion propagation implementations.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'mrender-audit-mpl'))

import numpy as np
from scipy.integrate import solve_ivp
from scipy.optimize import brentq
from scipy.spatial.transform import Rotation

import orbit_mechanics as om
import scene_common as sc
import mitsuba as mi
import satellite_orbit as so
import relative_motion as rm
import ground_observation as go
import optical_atmosphere as oa
import scene_earth as se
from config_loader import build_parser, build_render_config, resolve_primary_object
from optical_materials import create_kapton_mli_bsdf
from scene_objects import resolve_material_by_name
from yoshimulib.attitude.quaternion import q2dcm


def skew(v):
    x, y, z = v
    return np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])


def reference_orbit(el, t):
    """Bracketing Kepler solver + explicit active Rz(Omega) Rx(i) Rz(omega)."""
    a, e = el.semi_major_axis, el.eccentricity
    M = (el.mean_anomaly_0 + math.sqrt(om.EARTH_MU / a**3) * t) % (2 * math.pi)
    E = brentq(lambda x: x - e * math.sin(x) - M, 0., 2 * math.pi, xtol=1e-14)
    b = math.sqrt(1 - e*e)
    r = a * np.array([math.cos(E) - e, b * math.sin(E), 0.])
    v = math.sqrt(om.EARTH_MU / a) / (1 - e * math.cos(E)) * np.array(
        [-math.sin(E), b * math.cos(E), 0.])
    C = (Rotation.from_rotvec([0, 0, el.raan]).as_matrix()
         @ Rotation.from_rotvec([el.inclination, 0, 0]).as_matrix()
         @ Rotation.from_rotvec([0, 0, el.arg_periapsis]).as_matrix())
    return C @ r, C @ v


def attitude_metrics(moments, w0, dt, steps):
    """Joint integration of Cdot=C[omega]x and Euler's equation, no quaternion RHS."""
    I, Ii = sc.inertia_matrices(*moments)
    w0 = np.asarray(w0, dtype=float)
    def rhs(t, y):
        return np.r_[(y[:9].reshape(3, 3) @ skew(y[9:])).ravel(),
                     -Ii @ np.cross(y[9:], I @ y[9:])]
    sol = solve_ivp(rhs, [0, dt * steps], np.r_[np.eye(3).ravel(), w0],
                    method='DOP853', rtol=1e-11, atol=1e-13)
    if not sol.success:
        raise RuntimeError(sol.message)
    q, w = np.array([0., 0, 0, 1]), w0.copy()
    for _ in range(steps):
        q, w = sc.propagate_attitude(q, w, I, Ii, dt)
    C = q2dcm(q, scalar=4).T
    angle = Rotation.from_matrix(C @ sol.y[:9, -1].reshape(3, 3).T).magnitude()
    return {'error_deg': math.degrees(angle),
            'angular_momentum_vector_relative_error': float(np.linalg.norm(C @ I @ w - I @ w0)
                                                            / np.linalg.norm(I @ w0)),
            'energy_relative_error': float(abs(w @ I @ w - w0 @ I @ w0) / (w0 @ I @ w0))}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', type=Path)
    opts = parser.parse_args()
    results = []
    def check(name, fn, scope='active'):
        try:
            passed, details = fn()
            row = {'name': name, 'scope': scope, 'status': 'PASS' if passed else 'FAIL',
                   'details': details}
        except Exception as exc:
            row = {'name': name, 'scope': scope, 'status': 'ERROR',
                   'details': f'{type(exc).__name__}: {exc}'}
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    with tempfile.TemporaryDirectory(prefix='mrender_physics_audit_') as tmp:
        tmp = Path(tmp)
        el = om.OrbitalElements(7000., .1, .5, .3, .2, .1)

        def random_orbits():
            rng = np.random.default_rng(427)
            er, ev = [], []
            for _ in range(100):
                a = rng.uniform(7000, 50000)
                e = rng.uniform(.0001, min(.8, 1 - 6500/a))
                orbit = om.OrbitalElements(a, e, rng.uniform(.01, 3.13),
                                          *rng.uniform(0, 2*np.pi, 3))
                t = rng.uniform(-2, 2) * orbit.orbital_period
                r, v = om.compute_orbital_position(orbit, t)
                rr, vv = reference_orbit(orbit, t)
                er.append(np.linalg.norm(r - rr)); ev.append(np.linalg.norm(v - vv))
            return max(er) < 1e-6 and max(ev) < 1e-9, {
                'cases': 100, 'max_position_error_km': float(max(er)),
                'max_velocity_error_km_s': float(max(ev))}
        check('nondegenerate_kepler_reference', random_orbits)

        for name, orbit in [
            ('circular_argument_of_periapsis', om.OrbitalElements(7000, 0, .7, 0, np.pi/2, 0)),
            ('equatorial_longitude_of_node', om.OrbitalElements(10000, .1, 0, np.pi/2, .2, .3)),
        ]:
            def compare(orbit=orbit):
                r, v = om.compute_orbital_position(orbit, 0)
                rr, vv = reference_orbit(orbit, 0)
                err = float(np.linalg.norm(r-rr))
                return err < 1e-6, {'position_error_km': err,
                                    'actual_km': r.tolist(), 'reference_km': rr.tolist()}
            check(name, compare)

        def invariants():
            states = [om.compute_orbital_position(el, t) for t in np.linspace(0, el.orbital_period, 80)]
            energies = np.array([v@v/2 - om.EARTH_MU/np.linalg.norm(r) for r, v in states])
            hs = np.array([np.cross(r, v) for r, v in states])
            energy_err = float(np.ptp(energies)/abs(energies[0]))
            h_err = float(np.max(np.linalg.norm(hs-hs[0], axis=1))/np.linalg.norm(hs[0]))
            return max(energy_err, h_err) < 1e-12, {'energy_relative_spread': energy_err,
                                                    'momentum_relative_spread': h_err}
        check('kepler_conservation', invariants)

        for t in [1000., -10., -100.]:
            def numerical(t=t):
                p = om.NumericalPropagator(el, use_j2=False, use_drag=False)
                r, v = p.state_at(t)
                rr, vv = reference_orbit(el, t)
                err = float(np.linalg.norm(r-rr))
                return err < 1e-5, {'time_s': t, 'position_error_km': err}
            check(f'numerical_two_body_at_{t:g}s', numerical)

        def j2_gradient():
            r = np.array([7100., 1100., 2200.])
            def potential(r):
                d = np.linalg.norm(r)
                return (-om.EARTH_MU/d + om.EARTH_MU*om.J2*om.EARTH_RADIUS_KM**2
                        / (2*d**3) * (3*(r[2]/d)**2 - 1))
            h = .01
            ref = np.array([-(potential(r+h*e)-potential(r-h*e))/(2*h) for e in np.eye(3)])
            acc = om._eom_perturbed(0, np.r_[r, [0, 7, 1]], om.EARTH_MU,
                                    True, False, 2.2, .01, om.EARTH_RADIUS_KM)[3:]
            err = float(np.linalg.norm(acc-ref))
            return err < 1e-10, {'acceleration_error_km_s2': err}
        check('j2_potential_gradient', j2_gradient)

        def drag():
            r, v = om.compute_orbital_position(el, 0)
            r = r/np.linalg.norm(r)*(om.EARTH_RADIUS_KM+400)
            args = (0, np.r_[r, v], om.EARTH_MU, False)
            a0 = om._eom_perturbed(*args, False, 2.2, .01, om.EARTH_RADIUS_KM)[3:]
            a1 = om._eom_perturbed(*args, True, 2.2, .01, om.EARTH_RADIUS_KM)[3:]
            ref = -.5*om._atmospheric_density(400)*2.2*.01*np.linalg.norm(v*1000)*(v*1000)/1000
            err = float(np.linalg.norm((a1-a0)-ref))
            return err < 1e-17 and (a1-a0)@v < 0, {'acceleration_error_km_s2': err}
        check('drag_units_and_energy_loss', drag)

        def pointing():
            worst = 0.
            for e in [0., .2]:
                orbit = om.OrbitalElements(10000, e, .7, .4, 0, 0)
                for t in np.linspace(0, orbit.orbital_period, 20):
                    r, v = om.compute_orbital_position(orbit, t)
                    sun = np.array([1., .2, .1]); sun /= np.linalg.norm(sun)
                    for mode in ['nadir', 'sun_tracking', 'velocity_aligned']:
                        C = om.compute_attitude_matrix(r, v, mode, sun)
                        target = -r/np.linalg.norm(r) if mode == 'nadir' else sun
                        actual = C[:, 2]
                        if mode == 'velocity_aligned':
                            actual, target = C[:, 0], v/np.linalg.norm(v)
                        worst = max(worst, np.linalg.norm(C.T@C-np.eye(3)),
                                    abs(np.linalg.det(C)-1), np.linalg.norm(actual-target))
            return worst < 1e-12, {'max_basis_or_pointing_error': float(worst)}
        check('attitude_dcm_handedness_and_documented_axes', pointing)

        check('constant_spin_reference', lambda: (
            (m := attitude_metrics([2, 2, 2], [.2, .3, .4], .1, 10))['error_deg'] < 1e-7, m))
        for name, moments, w, dt, steps in [
            ('akatsuki_tumble', [4, 2, 1], [.3, .1, 2], 1/30, 119),
            ('absolute_preset_tumble', [2500, 8000, 7000], [.02, .005, .03], 5553.6/180, 179),
        ]:
            def tumble(moments=moments, w=w, dt=dt, steps=steps):
                m = attitude_metrics(moments, w, dt, steps)
                m['audit_angle_tolerance_deg'] = .05
                return m['error_deg'] < .05, m
            check(name, tumble)

        def zero_spin():
            I, Ii = sc.inertia_matrices(4, 2, 1)
            q, w = sc.propagate_attitude(np.array([0., 0, 0, 1]), np.zeros(3), I, Ii, .1)
            return bool(np.allclose(q, [0, 0, 0, 1]) and np.allclose(w, 0)), {'q': q.tolist()}
        check('zero_spin_finite', zero_spin)

        base_csv = 'time_s,a_km,e,inc_rad,raan_rad,ome_rad,f_rad'
        path = tmp/'orbit.csv'
        path.write_text(base_csv+'\n0,7000,0.1,0.7,0.2,0.3,0\n10,7000,0.1,0.7,0.2,0.3,0.1\n')
        quat_path = tmp/'attitude.csv'
        quat_path.write_text(base_csv+',q1,q2,q3,q4\n0,7000,0.1,0.7,0.2,0.3,0,0,0,0.7071067811865476,0.7071067811865476\n10,7000,0.1,0.7,0.2,0.3,0.1,0,0,-0.7071067811865476,-0.7071067811865476\n')
        eph = om.CsvEphemeris(str(quat_path))
        def csv_sign():
            C = eph.attitude_at(5)
            ref = Rotation.from_rotvec([0, 0, np.pi/2]).as_matrix()
            err = float(np.linalg.norm(C-ref))
            return err < 1e-12, {'matrix_error': err}
        check('csv_quaternion_sign_and_body_to_eci', csv_sign)

        def csv_wrap():
            p = tmp/'wrap.csv'
            p.write_text(base_csv+'\n0,7000,0.1,0.7,0.2,0.3,3.13\n10,7000,0.1,0.7,0.2,0.3,-3.13\n')
            ep = om.CsvEphemeris(str(p))
            r0, _ = ep.state_at(0); r1, _ = ep.state_at(10); rmiddle, _ = ep.state_at(5)
            err = float(np.linalg.norm(rmiddle-(r0+r1)/2))
            return err < 1., {'midpoint_vs_chord_km': err}
        check('csv_true_anomaly_wrap', csv_wrap)

        def csv_fallback():
            args = build_parser().parse_args([])
            args.csv_path = str(path); args.attitude_mode = 'nadir'
            spec = resolve_primary_object(args)
            s = so.compute_object_state(spec, 0, np.array([1., 0, 0]))
            angle = float(np.degrees(np.arccos(np.clip(s.attitude_matrix[:, 2] @
                          (-s.position_km/np.linalg.norm(s.position_km)), -1, 1))))
            return angle < 1e-6, {'nadir_error_deg': angle}
        check('csv_without_quaternion_uses_pointing_mode', csv_fallback)

        def absolute_csv():
            args = rm.parse_args(['--mode', 'absolute', '--chief-oe', '7000', '.1', '40', '0', '0', '0',
                                  '--deputy-orbit-csv', str(quat_path), '--deputy-attitude', 'csv'])
            ctx = rm.AbsoluteOrbitContext(args)
            C = ctx.deputy_attitude_dcm(5, np.eye(3), np.array([0., 0, 0, 1]), None)
            err = float(np.linalg.norm(C-eph.attitude_at(5).T))
            return err < 1e-12, {'matrix_error': err}
        check('absolute_mode_csv_attitude', absolute_csv)

        def composition():
            qc = Rotation.from_rotvec([0, 0, .8]).as_quat()
            qr = Rotation.from_rotvec([.6, 0, 0]).as_quat()
            p, q = rm.compose_deputy_state(np.ones(3), qc, np.array([2., 3, 4]), qr)
            ref = q2dcm(qr, scalar=4) @ q2dcm(qc, scalar=4)
            err = float(np.linalg.norm(q2dcm(q, scalar=4)-ref))
            return err < 1e-12, {'composition_matrix_error': err}
        check('relative_noncommuting_quaternion_composition', composition)

        def night_mask():
            scene = mi.load_dict({'type': 'scene', 'earth': se.create_earth()})
            mask = se.generate_night_mask(np.array([1., 0, 0]), 360, 180)
            values = []
            for x in [1., -1.]:
                si = scene.ray_intersect(mi.Ray3f(mi.Point3f(2*x, 0, 0), mi.Vector3f(-x, 0, 0)))
                uv = np.array(si.uv).ravel()
                values.append(float(mask[min(179, int(uv[1]*180)), min(359, int(uv[0]*360))]))
            return values[0] < .01 and values[1] > .99, {'sunward_mask': values[0],
                                                                       'antisunward_mask': values[1]}
        check('night_mask_matches_mitsuba_sphere_uv', night_mask)

        def panel_pointing():
            r, v = om.compute_orbital_position(el, 0)
            sun = np.array([1., 0, 0])
            C = om.compute_attitude_matrix(r, v, 'sun_tracking', sun)
            objects = so.create_satellite(np.zeros(3), C)
            transform = np.array(objects['solar_panel_left']['to_world'].matrix)
            lengths = np.linalg.norm(transform[:3, :3], axis=0)
            normal = transform[:3, int(np.argmin(lengths))]
            cosine = float(abs(normal@sun)/np.linalg.norm(normal))
            return cosine > .999, {'panel_normal_sun_cosine': cosine}
        check('sun_tracking_procedural_panel_normal', panel_pointing)

        def surface_occlusion():
            bad = []
            for lat in range(-89, 90):
                for lon in range(-180, 180, 5):
                    p = om.compute_observer_position_km(lat, lon, 0)
                    target = p*(1+400/om.EARTH_RADIUS_KM)
                    if om.is_earth_occluded(p, target):
                        bad.append([lat, lon])
            return not bad, {'false_occlusions': len(bad), 'cases': 179*72, 'examples': bad[:5]}
        check('surface_observer_overhead_visibility', surface_occlusion)

        def shadows():
            sun = np.array([1., 0, 0])
            flags = [so.is_in_earth_shadow(np.array(r, dtype=float), sun)
                     for r in [[7000, 0, 0], [-7000, 0, 0], [0, 7000, 0]]]
            return flags == [False, True, False], {'sunward_antisunward_side_shadow': flags}
        check('earth_shadow_basic_geometry', shadows)

        def rotating_target():
            args = build_parser().parse_args([])
            cfg = build_render_config(args, None)
            cfg.camera.view_mode = 'satellite'; cfg.camera.target_lat = 0.; cfg.camera.target_lon = 0.
            cfg.earth.earth_rotation = True
            cfg.earth.use_clouds = cfg.earth.use_night_lights = False
            t = cfg.earth.earth_rotation_period_hours*3600/4
            spec = resolve_primary_object(args)
            state = so.compute_object_state(spec, t, np.array([1., 0, 0]))
            _, target, _, _ = so.resolve_camera([state], cfg)
            rot, _, _ = so.build_earth_textures(tmp, 0, t, np.array([1., 0, 0]), cfg)
            ref = se.rotate_vector_z(om.compute_target_position_km(0, 0, 0), rot)*om.SCALE_FACTOR
            err = float(np.linalg.norm(np.asarray(target)-ref)/om.SCALE_FACTOR)
            return err < 1e-5, {'earth_rotation_deg': rot, 'ground_target_error_km': err}
        check('earth_fixed_camera_target_rotates_with_earth', rotating_target)

        def materials():
            names = ['aluminum_brushed', 'aluminum_polished', 'gold', 'solar_panel',
                     'thermal_blanket', 'radiator', 'carbon_composite', 'white_paint', 'kapton_mli']
            for name in names:
                mi.load_dict(resolve_material_by_name(name))
            return True, {'loaded_materials': len(names)}
        check('all_named_bsdfs_load', materials)

        def mli_weight():
            bsdf = mi.load_dict(create_kapton_mli_bsdf())
            si = mi.SurfaceInteraction3f(); si.wi = mi.Vector3f(0, 0, 1)
            wo = mi.Vector3f(.6, 0, .8)
            val = np.array(bsdf.eval(mi.BSDFContext(), si, wo)).ravel()
            diffuse = np.array([.8, .6, .2])*.8/np.pi
            fraction = float(np.mean(val/diffuse))
            return abs(fraction-.3) < 1e-6, {'measured_diffuse_fraction': fraction,
                                                       'documented_diffuse_fraction': .3}
        check('mli_documented_mirror_diffuse_mix', mli_weight)

        def sphere_flux():
            args = go.parse_args([])
            args.model_path = None; args.sphere_radius_m = 1.; args.sphere_albedo = .5
            basis = go.camera_basis(np.array([0., 0, 1.]))
            values = []
            for distance in [100., 200.]:
                img, _, span = go.render_target_irradiance(
                    args, basis, distance, np.array([0., 0, 1.]), np.ones(3),
                    mi.ScalarTransform4f(), .001, 1e-5, 512)
                flux = float(img.sum(axis=(0, 1)).mean())
                ref = (2/3)*args.sphere_albedo*(.001/distance)**2
                values.append({'range_km': distance, 'flux': flux, 'reference': ref,
                               'relative_error': abs(flux/ref-1)})
            return all(v['relative_error'] < .03 for v in values), values
        check('groundobs_lambert_sphere_flux_and_inverse_square', sphere_flux)

        def rebin_flux():
            rng = np.random.default_rng(7)
            img = rng.random((48, 48, 3))
            acc = go.accumulate_on_sensor_grid(img, .0001, 128, .000002)
            err = float(np.linalg.norm(img.sum(axis=(0, 1))-acc.sum(axis=(0, 1))))
            return err < 1e-9, {'flux_error': err}
        check('groundobs_sensor_rebin_flux_conservation', rebin_flux)

        def persistent():
            q0 = np.array([0., 0, 0, 1])
            q1 = Rotation.from_rotvec([.2, .3, .4]).as_quat()
            def scene(q):
                return rm.build_scene(rm.make_body_transform(np.zeros(3), q0),
                    rm.make_body_transform(np.array([1.5, 0., 0.]), q),
                    width=32, height=32, samples=32, show_inertial_axes=False, show_body_axes=False,
                    chief_model=None, chief_parts=None, chief_default_bsdf=None, chief_scale=1.,
                    deputy_model=None, deputy_parts=None, deputy_default_bsdf=None, deputy_scale=1.,
                    camera_origin=[3., 2., 2.], camera_target=[.75, 0., 0.], camera_fov=50.)
            p = rm.PersistentScene(); p.render(scene(q0))
            new = scene(q1)
            actual = np.array(p.render(new))
            reference = np.array(mi.render(mi.load_dict(new)))
            err = float(np.mean(abs(actual-reference)))
            return bool(np.isfinite(actual).all() and err < .001), {
                'mean_absolute_pixel_error': err, 'rebuilds': p.rebuilds, 'updates': p.updates}
        check('persistent_vs_rebuilt_scene_smoke', persistent)

        def render_pipeline():
            args = build_parser().parse_args([])
            cfg = build_render_config(args, None)
            cfg.width = cfg.height = 24
            cfg.lighting.sample_count = 8
            cfg.lighting.hdri_path = None
            cfg.earth.use_night_lights = cfg.earth.use_clouds = False
            cfg.earth.use_atmosphere = True
            spec = resolve_primary_object(args)
            so.render_frame(0, 2, [spec], cfg, tmp)
            image = np.array(mi.Bitmap(str(tmp/'frame_0000.png')))
            return bool(image.shape == (24, 24, 3) and image.max() > 0), {
                'shape': list(image.shape), 'max_uint8': int(image.max()),
                'atmosphere_enabled': True}
        check('render_frame_volpath_and_png_smoke', render_pipeline)

        def atmosphere():
            p = oa.AtmosphereParameters()
            _, actual = oa.compute_atmospheric_scattering(np.array([0., 0, p.earth_radius]),
                np.array([0., 0, 1.]), np.array([0., 0, 1.]), p, 4096)
            beta = np.array([oa.wavelength_dependent_rayleigh(w, reference_coeff=p.rayleigh_coeff)
                             for w in [680, 550, 440]])
            tau = beta*p.rayleigh_scale_height*(-np.expm1(-p.atmosphere_top/p.rayleigh_scale_height))
            tau += p.mie_coeff*p.mie_scale_height*(-np.expm1(-p.atmosphere_top/p.mie_scale_height))
            ref = np.exp(-tau)
            return bool(np.allclose(actual, ref, atol=1e-5)), {
                'actual_transmittance': actual.tolist(), 'beer_lambert_reference': ref.tolist()}
        check('analytic_atmosphere_beer_lambert', atmosphere, scope='unused_helper')

        def albedo():
            day = oa.compute_earth_albedo(np.array([7000., 0, 0]), np.array([-1., 0, 0]), np.array([1., 0, 0]))
            night = oa.compute_earth_albedo(np.array([-7000., 0, 0]), np.array([1., 0, 0]), np.array([1., 0, 0]))
            return bool(day[0] > 0 and night[0] == 0), {'day_rgb': day.tolist(), 'night_rgb': night.tolist()}
        check('analytic_earth_albedo_day_night', albedo, scope='unused_helper')

    summary = {'python': sys.version.split()[0], 'mitsuba': mi.__version__, 'variant': mi.variant(),
               'checks': len(results), 'passed': sum(r['status']=='PASS' for r in results),
               'failed': sum(r['status']!='PASS' for r in results), 'results': results}
    if opts.output:
        opts.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False)+'\n')
    print(f"Checks: {summary['checks']}; passed: {summary['passed']}; failed: {summary['failed']}")
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
