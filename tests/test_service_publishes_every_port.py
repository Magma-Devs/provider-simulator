"""Every port the topology binds must be published by the Kubernetes Service.

This is the check that was missing. Four provider pools were added to the
topology with listeners on 18588-18601. The Service in front of the simulator
was never extended, so from inside the cluster those ports did not exist. The
simulator's own /providers endpoint kept reporting all 51 providers, because it
reports its own configuration and knows nothing about the Service. Every router
dialling one of those pools answered "No pairings available", on both the
canonical and the local cluster, and nothing said why.

A port the Service publishes with nothing behind it refuses the connection,
which is loud and correct. A port the simulator binds and the Service omits is
silent. That asymmetry is why this test only checks one direction.
"""

import pathlib

import yaml

from provider_simulator.topology import TOPOLOGY

_K8S = pathlib.Path(__file__).resolve().parents[1] / "k8s"


def _topology_ports() -> set:
    return {
        endpoint[2]
        for row in TOPOLOGY
        for endpoint in row[-1]
    }


def _service_ports() -> set:
    spec = yaml.safe_load((_K8S / "service.yml").read_text())
    return {p["port"] for p in spec["spec"]["ports"]}


def _container_ports() -> set:
    spec = yaml.safe_load((_K8S / "deployment.yml").read_text())
    container = spec["spec"]["template"]["spec"]["containers"][0]
    return {p["containerPort"] for p in container["ports"]}


def test_the_service_publishes_every_port_the_topology_binds():
    missing = sorted(_topology_ports() - _service_ports())
    assert not missing, (
        f"the topology binds {missing} and k8s/service.yml does not publish "
        f"them, so nothing in the cluster can reach those providers. The "
        f"simulator will still report them on /providers, and every router "
        f"dialling them will answer 'No pairings available' with no clue why. "
        f"Add them to k8s/service.yml."
    )


def test_the_deployment_declares_every_port_the_topology_binds():
    missing = sorted(_topology_ports() - _container_ports())
    assert not missing, (
        f"the topology binds {missing} and k8s/deployment.yml does not declare "
        f"them as container ports. The process can still bind them, so this "
        f"alone does not break traffic, but the manifest then lies about what "
        f"the container serves. Add them to k8s/deployment.yml."
    )


def test_service_port_names_are_unique():
    """Two Service ports may not share a name; Kubernetes rejects the object.

    This does NOT check name length. The Kubernetes docs describe port names as
    IANA_SVC_NAME, at most 15 characters, but the deployed Service on the
    canonical cluster carries provider-solana-1, provider-solana-2 and
    provider-solana-3 at 17 characters each and has been serving for months.
    Asserting a limit the running cluster does not enforce would fail this test
    on configuration that demonstrably works.
    """
    spec = yaml.safe_load((_K8S / "service.yml").read_text())
    names = [p["name"] for p in spec["spec"]["ports"]]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate Service port names: {duplicates}"
