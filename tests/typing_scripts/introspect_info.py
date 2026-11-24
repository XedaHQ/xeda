"""Read what `xeda.introspect` knows about the flows: each listing has a typed shape."""

from typing import Literal, assert_type

from xeda import introspect

info = introspect.flow_info("yosys_fpga")
assert_type(info, introspect.FlowInfo)
assert_type(info["name"], str)
assert_type(info["description"], str | None)
assert_type(
    info["category"],
    Literal["simulation", "synthesis", "fpga_synthesis", "asic_synthesis", "other"],
)
assert_type(info["inputs"], list[introspect.FlowInputInfo])
for flow in introspect.flows_info():
    assert_type(flow, introspect.FlowInfo)
    for item in flow["inputs"]:
        assert_type(item["cardinality"], Literal["one", "optional", "many"])
        assert_type(item["producer"], str | None)
    for relation in flow["can_precede"]:
        assert_type(relation, introspect.FlowRelation)
        assert_type(relation["binds"], list[introspect.FlowBinding])
    print(flow.get("experimental", False))

settings = introspect.settings_info("yosys_fpga")
assert_type(settings, introspect.SettingsInfo)
for setting in settings["fields"]:
    assert_type(setting, introspect.SettingInfo)
    assert_type(setting["tier"], Literal["common", "advanced", "expert"])
    assert_type(setting.get("unit"), str | None)

results = introspect.results_info("yosys_fpga")
assert_type(results, introspect.ResultsInfo)
for metric in results["metrics"]:
    assert_type(metric, introspect.MetricDescription)
    assert_type(metric["unit"], str | None)
for artifact in results["artifacts"]:
    assert_type(artifact["when"], str)

for board in introspect.boards_info():
    assert_type(board, introspect.BoardInfo)
    assert_type(board["board"], str)
for platform in introspect.platforms_info():
    assert_type(platform, introspect.PlatformInfo)
    assert_type(platform["installed"], bool)
    assert_type(platform.get("directory"), str | None)
