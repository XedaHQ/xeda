from xeda.tool import Docker


def test_docker_cpuinfo_splits_at_every_blank_line(monkeypatch):
    """`re.split` once got `re.MULTILINE` as its `maxsplit`, so a machine with more than eight
    processors had its ninth and later entries left joined in the last one."""
    processors = 12
    text = "\n\n".join(f"processor\t: {n}\nmodel name\t: cpu" for n in range(processors)) + "\n\n"
    docker = Docker(image="any")
    monkeypatch.setattr(Docker, "run", lambda self, *args, **kwargs: text)
    assert docker.cpuinfo is not None
    entries = [entry for entry in docker.cpuinfo if any(entry)]
    assert len(entries) == processors
    assert all(len(entry) == 2 for entry in entries)
    assert docker.nproc >= processors
