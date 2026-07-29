"""Setup script for Vimm Bulk Downloader.

This is a fallback for environments where pyproject.toml's
flat-layout package discovery causes issues. It explicitly
defines the package structure.
"""
from setuptools import setup, find_packages

setup(
    name="vimm-bulk-downloader",
    version="1.0.0",
    description="Download multiple games from vimm.net concurrently with IP rotation",
    packages=find_packages(where="."),
    package_dir={"": "."},
    include_package_data=True,
    python_requires=">=3.10",
    install_requires=[
        "requests>=2.31.0",
        "beautifulsoup4>=4.12.0",
        "rich>=13.0.0",
        "colorama>=0.4.6",
        "stem>=1.8.0",
        "PySocks>=1.7.1",
    ],
    entry_points={
        "console_scripts": [
            "vimm-downloader=vimm_bulk_downloader.cli:main",
        ],
    },
)
