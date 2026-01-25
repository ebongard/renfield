"""
Renfield Satellite - Setup Script
"""

from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requirements = [
        line.strip()
        for line in f
        if line.strip() and not line.startswith("#")
    ]

setup(
    name="renfield-satellite",
    version="1.0.0",
    description="Raspberry Pi voice assistant satellite for Renfield",
    author="Renfield Team",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "renfield-satellite=renfield_satellite.main:run",
            "renfield-monitor=renfield_satellite.cli.monitor:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Operating System :: POSIX :: Linux",
        "Topic :: Home Automation",
    ],
)
