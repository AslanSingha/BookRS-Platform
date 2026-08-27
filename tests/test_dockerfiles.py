"""The test image must build on the same foundation as the service.

``docker/test.Dockerfile`` duplicates ``docker/recommend.Dockerfile``'s
build stage so that BuildKit reuses its layers instead of compiling
implicit twice. Duplication invites drift, and drift here is quiet: the
suite would keep passing while exercising a different dependency set
than the service runs on. A version skew in implicit or numpy would
show up as a production bug that no test could reproduce.

So the duplication is asserted rather than trusted.
"""

from pathlib import Path

DOCKER = Path(__file__).resolve().parent.parent / "docker"


def build_stage(name: str) -> list[str]:
    """Instruction lines of the first stage, comments and blanks removed."""
    path = DOCKER / name
    # Not a skip. These guards exist to catch drift, and a guard that
    # quietly stands down when it cannot find its subject is the same
    # false assurance it was written to prevent — which is exactly what
    # happened the first time this ran, in a container where docker/
    # was not mounted.
    assert path.exists(), f"{path} not readable; is docker/ mounted?"

    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # The second FROM opens the runtime stage; stop there.
        if line.upper().startswith("FROM") and lines:
            break
        lines.append(line)
    return lines


def test_build_stages_are_identical():
    recommend = build_stage("recommend.Dockerfile")
    test = build_stage("test.Dockerfile")
    assert recommend == test, (
        "docker/test.Dockerfile's build stage has drifted from "
        "docker/recommend.Dockerfile's. The test image would then be "
        "built on different dependencies than the service runs on, and "
        "a green suite would mean less than it appears to. Change both "
        "together, or give them a shared base."
    )


def test_test_image_provides_a_javascript_runtime():
    """The widget tests skip without node, and a suite that only ever
    skips reports a confidence it has not earned."""
    path = DOCKER / "test.Dockerfile"
    assert path.exists(), f"{path} not readable; is docker/ mounted?"
    assert "nodejs" in path.read_text(encoding="utf-8")


def test_service_images_carry_no_javascript_runtime():
    """node belongs in the test image and nowhere else. A service image
    acquiring one means the separation has quietly collapsed."""
    names = ("recommend.Dockerfile", "api.Dockerfile",
             "embedding.Dockerfile", "ingestion.Dockerfile")
    for name in names:
        path = DOCKER / name
        assert path.exists(), f"{path} not readable; is docker/ mounted?"
        installs = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if "nodejs" in line and not line.strip().startswith("#")
        ]
        assert not installs, f"{name} installs a JavaScript runtime"
