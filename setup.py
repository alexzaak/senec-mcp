from setuptools import find_packages, setup

setup(
    name="senec-mcp",
    version="1.0.0",
    description="MCP server and CLI for SENEC.Home battery storage (local device + mein-senec.de)",
    packages=find_packages(include=["senec_mcp", "senec_mcp.*"]),
    package_data={"senec_mcp": ["skills/*.md", "README.md", "tests/TEST.md"]},
    install_requires=["click>=8.0", "prompt_toolkit>=3.0", "mcp>=1.2,<2"],
    extras_require={"dev": ["pytest>=7"]},
    entry_points={"console_scripts": [
        "senec=senec_mcp.cli:main",
        "senec-mcp=senec_mcp.mcp_server:main",
    ]},
    python_requires=">=3.10",
)
