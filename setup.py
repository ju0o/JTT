"""Build the JTT distribution under a package namespace unique to JTT.

The maintained source tree keeps the historical ``tmux_agent_tower`` import
path for development and Legacy Tower tests. Wheels expose the same code as
``ju0_terminal_tower`` so the new distribution does not install files over
the published ``tmux-agent-tower`` package.
"""

from setuptools import find_packages, setup


SOURCE_PACKAGE = "tmux_agent_tower"
DIST_PACKAGE = "ju0_terminal_tower"
source_packages = find_packages(
    where="src",
    include=[SOURCE_PACKAGE, SOURCE_PACKAGE + ".*"],
)
packages = [DIST_PACKAGE + package[len(SOURCE_PACKAGE):] for package in source_packages]
package_dir = {
    DIST_PACKAGE + package[len(SOURCE_PACKAGE):]: "src/" + package.replace(".", "/")
    for package in source_packages
}

if not source_packages or packages[0] != DIST_PACKAGE:
    raise RuntimeError("JTT source package discovery did not find the expected root")

setup(packages=packages, package_dir=package_dir)
