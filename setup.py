from setuptools import find_packages, setup

setup(
    name="rcloud",
    version="0.1.0",
    packages=find_packages(include=["rcloud", "rcloud.*"]),
    python_requires=">=3.9",
    install_requires=["psutil>=5.9"],
    extras_require={"tray": ["pystray>=0.19", "Pillow>=10.0"]},
    entry_points={"console_scripts": [
        "rcloud-controller=rcloud.cli:main",
        "rcloud-worker=rcloud.worker_app:main",
    ]},
)
