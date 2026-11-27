from setuptools import find_namespace_packages, setup

setup(
    name="cli-anything-senec",
    version="1.0.0",
    description="Agent-native CLI harness for the SENEC.Home battery storage web UI",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    package_data={"cli_anything.senec": ["skills/*.md", "README.md", "tests/TEST.md"]},
    install_requires=["click>=8.0", "prompt_toolkit>=3.0"],
    extras_require={"dev": ["pytest>=7"], "mcp": ["mcp>=1.2,<2"]},
    entry_points={"console_scripts": [
        "cli-anything-senec=cli_anything.senec.senec_cli:main",
        "senec-mcp=cli_anything.senec.mcp_server:main [mcp]",
    ]},
    python_requires=">=3.10",
)
