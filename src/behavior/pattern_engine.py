"""Deterministic train-only standardization and farthest-first k-means."""
import numpy as np
from .models import FEATURES, BehaviorConfig

class PatternModel:
    def __init__(self, config=None):
        self.config = config or BehaviorConfig()
        self.centers = None
    def fit(self, segments):
        if not segments:
            self.centers = np.empty((0, len(FEATURES)))
            self.mean = np.zeros(len(FEATURES)); self.scale = np.ones(len(FEATURES))
            return self
        x = np.array([[s["features"][k] for k in FEATURES] for s in segments], dtype=float)
        if not np.isfinite(x).all():
            raise ValueError("nonfinite_features")
        self.mean, self.scale = x.mean(axis=0), x.std(axis=0)
        self.scale[self.scale < 1e-12] = 1
        z = (x-self.mean)/self.scale
        k = max(1, min(self.config.max_clusters, len(x)//self.config.normal_samples))
        centers = [z[0]]
        while len(centers) < k:
            distances = np.min(((z[:,None,:]-np.array(centers)[None,:,:])**2).sum(axis=2), axis=1)
            if distances.max() < 1e-12:
                break
            centers.append(z[int(np.argmax(distances))])
        centers = np.array(centers)
        for _ in range(50):
            labels = np.argmin(((z[:,None,:]-centers[None,:,:])**2).sum(axis=2), axis=1)
            updated = np.array([z[labels==j].mean(axis=0) if np.any(labels==j) else centers[j] for j in range(len(centers))])
            if np.allclose(updated, centers, atol=1e-9):
                break
            centers = updated
        self.centers = centers
        return self
    def predict(self, segment):
        if self.centers is None:
            raise ValueError("model_not_fitted")
        if not len(self.centers):
            return None, 0.0
        z = (np.array([segment["features"][k] for k in FEATURES])-self.mean)/self.scale
        distance = np.sqrt(((self.centers-z)**2).mean(axis=1))
        label = int(np.argmin(distance))
        return f"P{label:02d}", float(np.exp(-distance[label]))
    def payload(self):
        return dict(feature_names=list(FEATURES), mean=self.mean.tolist(), scale=self.scale.tolist(),
                    centers=self.centers.tolist(), method="deterministic_farthest_first_kmeans")
