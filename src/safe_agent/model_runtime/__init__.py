"""Explicit Docker-only offline text-model isolation boundary."""

from .docker_transport import DockerIsolatedTextGenerator, ModelIsolationDenied, ModelIsolationPolicy

__all__ = ["DockerIsolatedTextGenerator", "ModelIsolationDenied", "ModelIsolationPolicy"]
