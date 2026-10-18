import numpy as np
from scipy.interpolate import RBFInterpolator


class MathEngine:
    """
    Silnik obliczeniowy oparty na numpy.linalg.lstsq oraz scipy.interpolate.
    """

    @staticmethod
    def helmert_transform(pts_old: np.ndarray, pts_ref: np.ndarray) -> dict:
        n = pts_old.shape[0]
        if n < 2:
            raise ValueError("Transformacja Helmerta wymaga minimum 2 punktów.")

        A = np.zeros((2 * n, 4))
        L = np.zeros(2 * n)

        A[0::2, 0] = 1.0
        A[0::2, 2] = pts_old[:, 0]
        A[0::2, 3] = -pts_old[:, 1]
        L[0::2] = pts_ref[:, 0]

        A[1::2, 1] = 1.0
        A[1::2, 2] = pts_old[:, 1]
        A[1::2, 3] = pts_old[:, 0]
        L[1::2] = pts_ref[:, 1]

        params, _, _, _ = np.linalg.lstsq(A, L, rcond=None)
        tx, ty, a, b = params

        scale = float(np.hypot(a, b))
        rotation_rad = float(np.arctan2(b, a))
        rotation_deg = float(np.degrees(rotation_rad))

        pts_transformed = np.zeros_like(pts_ref)
        pts_transformed[:, 0] = tx + a * pts_old[:, 0] - b * pts_old[:, 1]
        pts_transformed[:, 1] = ty + b * pts_old[:, 0] + a * pts_old[:, 1]

        residuals = pts_ref - pts_transformed
        errors = np.linalg.norm(residuals, axis=1)
        rmse = float(np.sqrt(np.mean(errors ** 2)))

        return {
            "type": "Helmert (4 parametry)",
            "params": {"tx": float(tx), "ty": float(ty), "a": float(a), "b": float(b)},
            "scale_x": scale,
            "scale_y": scale,
            "rotation_deg": rotation_deg,
            "pts_transformed": pts_transformed,
            "residuals": residuals,
            "errors": errors,
            "rmse": rmse,
            "max_error": float(np.max(errors)),
            "min_error": float(np.min(errors)),
        }

    @staticmethod
    def affine_transform(pts_old: np.ndarray, pts_ref: np.ndarray) -> dict:
        n = pts_old.shape[0]
        if n < 3:
            raise ValueError("Transformacja afiniczna wymaga minimum 3 punktów.")

        A = np.column_stack([np.ones(n), pts_old[:, 0], pts_old[:, 1]])

        params_x, _, _, _ = np.linalg.lstsq(A, pts_ref[:, 0], rcond=None)
        params_y, _, _, _ = np.linalg.lstsq(A, pts_ref[:, 1], rcond=None)

        tx, a, b = params_x
        ty, c, d = params_y

        pts_transformed = np.column_stack([A @ params_x, A @ params_y])

        residuals = pts_ref - pts_transformed
        errors = np.linalg.norm(residuals, axis=1)
        rmse = float(np.sqrt(np.mean(errors ** 2)))

        scale_x = float(np.hypot(a, c))
        scale_y = float(np.hypot(b, d))
        rotation_deg = float(np.degrees(np.arctan2(c, a)))

        shear_deg = 0.0
        if scale_x > 0 and scale_y > 0:
            sin_shear = np.clip((a * b + c * d) / (scale_x * scale_y), -1.0, 1.0)
            shear_deg = float(np.degrees(np.arcsin(sin_shear)))

        return {
            "type": "Afiniczna (6 parametrów)",
            "params": {
                "tx": float(tx), "ty": float(ty),
                "a": float(a), "b": float(b), "c": float(c), "d": float(d)
            },
            "scale_x": scale_x,
            "scale_y": scale_y,
            "rotation_deg": rotation_deg,
            "shear_deg": shear_deg,
            "pts_transformed": pts_transformed,
            "residuals": residuals,
            "errors": errors,
            "rmse": rmse,
            "max_error": float(np.max(errors)),
            "min_error": float(np.min(errors)),
        }

    @staticmethod
    def calculate_local_grid_deformation(pts_old: np.ndarray, residuals: np.ndarray, grid_pts: np.ndarray) -> np.ndarray:
        if len(pts_old) < 3:
            return np.zeros_like(grid_pts)

        rbf_x = RBFInterpolator(pts_old, residuals[:, 0], kernel='thin_plate_spline')
        rbf_y = RBFInterpolator(pts_old, residuals[:, 1], kernel='thin_plate_spline')

        dx = rbf_x(grid_pts)
        dy = rbf_y(grid_pts)

        return np.column_stack([dx, dy])

    @staticmethod
    def calculate_local_scale_and_rotation_grids(pts_old: np.ndarray, res: dict, nx: int = 40, ny: int = 40):
        """
        Oblicza siatki 2D lokalnej skali [m/px] oraz lokalnej rotacji [stopnie]
        w przestrzeni mapy dawnej za pomocą pochodnych cząstkowych (Jakobianu) odwzorowania RBF.
        """
        old_min_x, old_min_y = np.min(pts_old, axis=0)
        old_max_x, old_max_y = np.max(pts_old, axis=0)

        x_1d = np.linspace(old_min_x, old_max_x, nx)
        y_1d = np.linspace(old_min_y, old_max_y, ny)
        X_old, Y_old = np.meshgrid(x_1d, y_1d)

        grid_pts = np.column_stack([X_old.ravel(), Y_old.ravel()])

        # 1. Transformacja globalna
        p = res["params"]
        if "Helmert" in res["type"]:
            tx, ty, a, b = p["tx"], p["ty"], p["a"], p["b"]
            gx = tx + a * grid_pts[:, 0] - b * grid_pts[:, 1]
            gy = ty + b * grid_pts[:, 0] + a * grid_pts[:, 1]
        else:
            tx, ty, a, b, c, d = p["tx"], p["ty"], p["a"], p["b"], p["c"], p["d"]
            gx = tx + a * grid_pts[:, 0] + b * grid_pts[:, 1]
            gy = ty + c * grid_pts[:, 0] + d * grid_pts[:, 1]

        # 2. Odkształcenia lokalne RBF
        local_def = MathEngine.calculate_local_grid_deformation(pts_old, res["residuals"], grid_pts)

        X_ref = (gx + local_def[:, 0]).reshape((ny, nx))
        Y_ref = (gy + local_def[:, 1]).reshape((ny, nx))

        # 3. Różniczkowanie numeryczne (Jakobian przekształcenia)
        # Oś 1 to X (kolumny), oś 0 to Y (wiersze)
        dX_dy, dX_dx = np.gradient(X_ref, y_1d, x_1d)
        dY_dy, dY_dx = np.gradient(Y_ref, y_1d, x_1d)

        # Skala wzdłuż osi X i Y oraz uśredniona skala powierzchniowa (z wyznacznika Jakobianu)
        sx = np.hypot(dX_dx, dY_dx)
        sy = np.hypot(dX_dy, dY_dy)
        det = np.abs(dX_dx * dY_dy - dX_dy * dY_dx)
        scale_grid = np.sqrt(np.maximum(det, 1e-12))  # [m / px]

        # Lokalny kąt obrotu (w stopniach)
        rot_grid = np.degrees(np.arctan2(dY_dx, dX_dx))

        return X_old, Y_old, X_ref, Y_ref, scale_grid, rot_grid