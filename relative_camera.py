"""Camera geometry in the chief-centered Hill frame (scene units: km)."""
import numpy as np

CAMERA_MODES = ('manual', 'chief_to_deputy', 'chief_fixed', 'deputy_to_chief', 'deputy_fixed')


def chief_origin(position):
    p = np.asarray(position, dtype=float)
    if p.shape != (3,) or not np.all(np.isfinite(p)) or np.any(p != 0):
        raise ValueError('chief はHill座標系の原点 [0, 0, 0] に固定です。deputy の相対位置を指定してください。')
    return np.zeros(3)


def resolve_camera(mode, chief_transform, deputy_transform, *, origin, target, up,
                   offset=(0, 0, 0), direction=(1, 0, 0), body_up=(0, 0, 1)):
    if mode not in CAMERA_MODES:
        raise ValueError(f'未対応のカメラモード: {mode}')
    if mode == 'manual':
        return {'origin': list(origin), 'target': list(target), 'up': list(up)}
    chief, deputy = np.asarray(chief_transform, dtype=float), np.asarray(deputy_transform, dtype=float)
    host, other = (chief, deputy) if mode.startswith('chief_') else (deputy, chief)
    rotation = host[:3, :3]
    eye = host[:3, 3] + rotation @ np.asarray(offset, dtype=float)
    if mode.endswith('_fixed'):
        look = eye + rotation @ np.asarray(direction, dtype=float)
    else:
        look = other[:3, 3]
    forward = look - eye
    if not np.all(np.isfinite(forward)) or np.linalg.norm(forward) < 1e-12:
        raise ValueError('カメラ位置と注視点が一致しています。相対位置・カメラ取付位置または固定方向を変更してください。')
    forward /= np.linalg.norm(forward)
    up_world = rotation @ np.asarray(body_up, dtype=float)
    up_world -= forward * np.dot(up_world, forward)
    if np.linalg.norm(up_world) < 1e-9:
        # Stable finite basis even when the target crosses the camera's up axis.
        up_world = np.eye(3)[np.argmin(np.abs(forward))]
        up_world -= forward * np.dot(up_world, forward)
    up_world /= np.linalg.norm(up_world)
    return {'origin': eye.tolist(), 'target': look.tolist(), 'up': up_world.tolist()}
