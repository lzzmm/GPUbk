from importlib.metadata import PackageNotFoundError, version


MIN_SETUPTOOLS_MAJOR = 77


def _require_setuptools(build_version: str) -> None:
    try:
        major = int(build_version.partition(".")[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"cannot verify the setuptools build backend version {build_version!r}"
        ) from exc
    if major < MIN_SETUPTOOLS_MAJOR:
        raise RuntimeError(
            "GPUbk source builds require setuptools>=77, but this installer loaded "
            f"setuptools {build_version}. Upgrade pip before installing from source, "
            "or install the published GPUbk wheel."
        )


def _main() -> None:
    try:
        build_version = version("setuptools")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "GPUbk source builds require setuptools>=77; upgrade pip before installing "
            "from source, or install the published GPUbk wheel."
        ) from exc
    _require_setuptools(build_version)

    from setuptools import setup

    setup()


if __name__ == "__main__":
    _main()
