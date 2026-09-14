#!/usr/bin/env python3
"""Cross-check this busway document/acceptance fixture against supplied sources.

Run from any directory: python3 scripts/verify_busway_knowledge_materials.py
Requires PyYAML in the selected authoring Python; does not alter app dependencies.
This is an offline source audit, not an application, retrieval, or diagnostic test.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import yaml


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the completed audit JSON.")
    args = parser.parse_args()
    cases = json.loads((ROOT / "busway_files/acceptance/busway-retrieval-cases.json").read_text())
    knowledge_path = ROOT / cases["knowledge_document"]["path"]
    knowledge = knowledge_path.read_text()
    inputs = []
    for source in cases["original_sources"]:
        actual = digest(ROOT / source["path"])
        require(actual == source["sha256"], f"Source version changed: {source['path']}")
        require(actual in knowledge, f"Missing source checksum in knowledge: {source['id']}")
        inputs.append({**source, "verified": True})
    require(digest(knowledge_path) == cases["knowledge_document"]["sha256"], "Knowledge changed; review and regenerate evidence locations.")
    require(digest(ROOT / cases["topology_document"]["path"]) == cases["topology_document"]["sha256"], "Topology upload document version changed.")

    xml = ET.parse(ROOT / "new_files/topology.xml").getroot()
    topology = json.loads((ROOT / "busway_files/topology.source.json").read_text())
    instances = yaml.safe_load((ROOT / "new_files/instances.diagnostic.yaml").read_text())
    mapping = yaml.safe_load((ROOT / "new_files/mapping.topology_xml.yaml").read_text())["materialization"]["topology_xml"]
    bindings = yaml.safe_load((ROOT / "new_files/point_mapping.yaml").read_text())
    rule = (ROOT / "new_files/rule_v2.md").read_text()
    project = xml.findtext("ProjectSettings/ProjectName")
    require(project == "PBMBaseProj_2512", "This is a versioned fixture for PBMBaseProj_2512.")
    require(topology["metadata"]["source"]["sha256"] == digest(ROOT / "new_files/topology.xml"), "JSON projection source hash differs.")
    assets = {a["full_code"]: a for a in topology["assets"]}
    require(len(assets) == len(topology["assets"]) == 24, "Asset identity/count mismatch.")
    source_nodes = {}
    parents = {}

    def walk(node: ET.Element, parent: str | None = None) -> None:
        code = node.attrib["FullCode"]
        require(code not in source_nodes, f"Duplicate source asset {code}")
        source_nodes[code] = node
        parents[code] = parent
        for child in node.findall("Children/Child"):
            walk(child, code)

    walk(xml.find("NodeTree/Root"))
    require(set(source_nodes) == set(assets), "XML and JSON assets differ.")
    xml_ports, xml_points, xml_annotations = {}, {}, {}
    for code, node in source_nodes.items():
        asset = assets[code]
        ref = f"{project}/{code}"
        require(asset["asset_ref"] == ref, f"Asset ref mismatch: {code}")
        matching_types = {
            kind
            for kind, definition in mapping["entity_mappings"].items()
            if definition["selector"].get("attributes")
            and all(
                node.attrib.get(key) == value
                for key, value in definition["selector"]["attributes"].items()
            )
        }
        require(matching_types == {asset["entity_type"]}, f"Asset type differs from source mapping: {code}")
        expected_parent = f"{project}/{parents[code]}" if parents[code] else None
        require(asset["parent_asset_ref"] == expected_parent, f"Parent mismatch: {code}")
        # Verify every retained business field against XML, not against prose.
        for attribute, field in topology["metadata"]["projection"]["asset_attributes"].items():
            require(asset.get(field) == node.attrib.get(attribute), f"Asset projection mismatch: {code}.{field}")
        annotations = {item.attrib["Data1"]: item.attrib["Data2"] for item in node.findall("Extension/Data")}
        require(asset.get("source_annotations", {}) == annotations, f"Source annotations mismatch: {code}")
        xml_annotations[code] = annotations
        for port in node.findall("Ports/Port"):
            key = f"{ref}/port/{int(port.attrib['Ordinal'])}"
            xml_ports[key] = (ref, int(port.attrib["Ordinal"]), port.attrib["Name"], port.attrib["DisplayName"], port.attrib["PortType"], port.attrib["Enabled"] == "true", port.attrib.get("Load"))
        for point in node.findall("Variables/Variable"):
            key = f"{ref}#{point.attrib['Item']}"
            require(key not in xml_points, f"Duplicate source point {key}")
            xml_points[key] = (ref, point.attrib["Item"], point.attrib["Name"], point.attrib["Unit"], point.attrib.get("Comment"))
    projected_ports = {p["port_ref"]: (p["owner_asset_ref"], p["ordinal"], p["name"], p["display_name"], p["port_type_code"], p["enabled"], p.get("source_load_full_code")) for p in topology["ports"]}
    projected_points = {p["point_ref"]: (p["owner_asset_ref"], p["item"], p["variable_name"], p["measurement_system_ref"], p.get("source_comment")) for p in topology["measurement_points"]}
    require(xml_ports == projected_ports and len(projected_ports) == len(topology["ports"]) == 51, "Ports differ between XML and JSON.")
    require(xml_points == projected_points and len(projected_points) == len(topology["measurement_points"]) == 64, "All 64 points must exactly match XML.")
    require(sum(bool(p[-1]) for p in xml_ports.values()) == 33, "Nonempty Port.Load count differs.")
    require(
        Counter(a["entity_type"] for a in assets.values())
        == {"Project": 1, "Busway": 2, "Section": 2, "Joint": 9,
            "StraightSection": 7, "Flange": 1, "PlugInUnit": 1, "EndFeedUnit": 1},
        "Fixture asset-type counts changed.",
    )

    grouped = defaultdict(set)
    resolved = Counter()
    resolution = mapping["observable_resolution"]["mappings"]
    entities = instances["entities"]
    observable_ids = {key for key, e in entities.items() if e["type"] in {"Observable", "DerivedObservable"}}
    require(len(observable_ids) == 38, "Observable vocabulary changed.")
    for key in observable_ids:
        require(f"`{key}`" in knowledge, f"Observable missing from knowledge: {key}")
    for _, (owner, item, _, system, _) in xml_points.items():
        code = owner.split("/", 1)[1]
        grouped[code].add(item)
        selected = set()
        attrs = {"Item": item, "Unit": system}
        for target, entry in resolution.items():
            for match in entry["matches"]:
                exact = all(attrs.get(k) == v for k, v in match.get("variable_attributes", {}).items())
                prefixes = all(any(attrs.get(k, "").startswith(prefix) for prefix in choices) for k, choices in match.get("variable_attribute_prefixes", {}).items())
                owner_match = all(source_nodes[code].attrib.get(k) == v for k, v in match.get("owner_attributes", {}).items())
                if exact and prefixes and owner_match:
                    selected.add(target)
        require(len(selected) == 1, f"Observable mapping is missing or ambiguous: {code}#{item}")
        target = selected.pop()
        unit = entities[target]["canonical_unit"]
        require(str(bindings["components"][code]["points"][item]["unit"]) == str(unit), f"Semantic unit disagrees with supplied binding: {code}#{item}")
        resolved[target] += 1
    # These are acceptance fixture expectations, not platform-wide invariants.
    require(grouped["BUS1.S1.JPK1"] == grouped["BUS1.S1.JPK2"] == {"Battery", "Humidity", "Temp"}, "BUS1 Etemp coverage changed.")
    require(grouped["BUS1.S1.JPK3"] == {"Temp", "TempDiff", "TempDiffRate"}, "BUS1 EDock coverage changed.")
    require(grouped["BUS1.S1.PIU4"] == {"Ia", "Ib", "Ic", "PFTot", "kVArTot", "kWTot"}, "PIU4 coverage changed.")
    require(len(grouped["BUS2.S1.ETB1"]) == 25, "ETB1 point count changed.")
    for i in range(1, 7):
        require(grouped[f"BUS2.S1.JPK{i}"] == {"Temp", "TempDiff", "TempDiffRate", "WaterAlarm"}, "BUS2 joint coverage changed.")
    require(resolved["busway_body_absolute_temperature"] == 2 and resolved["joint_absolute_temperature"] == 7 and resolved["temperature_rise"] == resolved["temperature_rise_rate"] == 7, "Temperature semantics changed.")

    # Validate prose table units against actual source entities.
    table_rows = [line for line in knowledge.splitlines() if line.startswith("|")]
    table_checked = 0
    for key in observable_ids:
        rows = [row for row in table_rows if f"`{key}`" in row]
        if rows:
            require(any(row.split("|")[-2].strip() == str(entities[key]["canonical_unit"]) for row in rows), f"Wrong unit in knowledge table: {key}")
            table_checked += 1
    require(table_checked == 33, "Expected all 33 nontemperature prose-table unit entries.")

    candidates = defaultdict(list)
    for rid, relation in instances["relations"].items():
        if relation["type"] == "POSSIBLE_CAUSE":
            require(relation["assertion"] == "possible", f"Candidate is not marked possible: {rid}")
            candidates[relation["from"]["entity"]].append(relation["to"]["entity"])
    require(sum(map(len, candidates.values())) == 19, "Candidate relationship count changed.")
    require("THREE_PHASE_IMBALANCE" not in candidates, "Unexpected three-phase root-cause candidates.")
    for phenomenon, causes in candidates.items():
        rows = [row for row in table_rows if f"`{phenomenon}`" in row and "PCL_" in row]
        require(len(rows) == 1, f"Candidate table missing/duplicated: {phenomenon}")
        actual_names = rows[0].split("|")[2].strip().split("、")
        source_names = [entities[cause]["display_name"] for cause in causes]
        require(sorted(actual_names) == sorted(source_names), f"Candidate set differs from YAML: {phenomenon}")

    produced = {r["rule_clause"]: r["from"]["entity"] for r in instances["relations"].values() if r["type"] == "EVALUATED_BY" and r.get("clause_role") == "produces_phenomenon"}
    require(produced == {"D1": "TEMP_THRESHOLD_EXCEEDED", "D2": "CURRENT_LOAD_ABNORMAL", "D3": "JOINT_TEMP_DIFF", "D4": "WATER_ALARM", "D5": "TEMP_RISE_ABNORMAL", "D6": "THREE_PHASE_IMBALANCE"}, "Rule-to-phenomenon bindings changed.")
    for clause, entity in produced.items():
        require(any(f"`{entity}`" in row and f"| {clause}；" in row for row in table_rows), f"Rule binding missing in knowledge: {clause}")
    require(all(e["parameter_binding_status"] == "unresolved" for e in entities.values() if e["type"] == "DiagnosticParameter"), "Parameter bindings changed.")
    require(sum(e["type"] == "DiagnosticParameter" for e in entities.values()) == 11, "Parameter vocabulary changed.")
    require(entities["BUSWAY_INCIDENT_CAUSE_DISTRIBUTION"]["not_for_diagnostic_scoring"] is True, "Statistic scoring boundary changed.")
    # Verify the documented differences are actually present in the pinned sources.
    require(entities["THREE_PHASE_IMBALANCE"]["diagnostic_scope_status"] == "outside_current_product_scope", "SRC-01 requires rereview.")
    require("C5 条件1" in entities["LOCAL_HOTSPOT"]["rule_evaluation_note"], "SRC-02 requires rereview.")
    require('C5 条件1' in instances["relations"]["SCP_020"]["rule_basis"], "SRC-03 requires rereview.")
    require(instances["relations"]["SCP_010"]["rule_clause"] == "C5", "SRC-04 requires rereview.")
    for marker in ["SRC-01", "SRC-02", "SRC-03", "SRC-04"]:
        require(marker in knowledge, f"Undisclosed source difference: {marker}")
    c3, c4, c5 = (rule.split(title, 1)[1].split("\n##", 1)[0] for title in ["### 6.3 C3", "### 6.4 C4", "### 6.5 C5"])
    require("`j` 为局部热点" in c3 and "`j` 为局部热点" in c4, "C3/C4 hotspot definitions changed.")
    require("| 条件 4 | 集合 `S` 不存在局部热点 |" in c5, "C5 no-hotspot clause changed.")
    require("热点数量 `<5`" in c4 and "当前 mock 为 BUS2.S1 的 JPK1～JPK6" in c5, "Mock scope/hotspot-count source changed.")

    # Each cited knowledge range and hash must match the actual final document.
    headers = list(re.finditer(r"^## (K\d\d) (.+)$", knowledge, re.M))
    sections = {}
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(knowledge)
        sections[match[1]] = (knowledge[:match.start()].count("\n") + 1, knowledge[:end].count("\n"), hashlib.sha256(knowledge[match.start():end].encode()).hexdigest())
    require(len(sections) == 24 and cases["case_count"] == len(cases["cases"]) == 24, "Expected 24 sections/cases.")
    for case in cases["cases"]:
        require(case["expected_evidence_points"] and case["original_source_locators"], f"Case lacks evidence: {case['id']}")
        for evidence in case["expected_evidence"]:
            require(sections[evidence["section_id"]] == (evidence["line_start"], evidence["line_end"], evidence["sha256"]), f"Stale evidence range: {case['id']}")
    require(cases["ui_mode"] == "evidence_retrieval", "Do not treat retrieval as an enabled answer generator.")
    require(not any(token in knowledge for token in ["T:MV;", "source_device_id", "STG-PBM2512-", "staging/runtime/timeseries/"]), "Unexpected physical binding/deployment data in upload document.")

    report = {
        "status": "passed_offline_source_audit", "verified_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_document": cases["knowledge_document"], "original_sources": inputs,
        "topology_document": cases["topology_document"],
        "auxiliary_unit_crosscheck": {"path": "new_files/point_mapping.yaml", "sha256": digest(ROOT / "new_files/point_mapping.yaml"), "role": "unit verification only; physical binding contents not copied into knowledge"},
        "counts": {"assets": 24, "ports": 51, "nonempty_port_load_records": 33, "logical_points": 64, "source_observable_targets": len(resolved), "observable_concepts": 38, "candidate_relationships": 19, "phenomenon_rule_bindings": 6, "diagnostic_parameters": 11, "disclosed_source_differences": 4, "acceptance_cases": 24},
        "semantic_point_distribution": dict(sorted(resolved.items())),
        "checks": ["XML/JSON all asset business fields and parent identities", "XML/JSON all ports and all 64 point values", "all 64 source point observable mappings and canonical units versus supplied binding file", "all 19 candidate edges versus YAML and knowledge table", "all six D-class phenomenon bindings", "unbound parameters and statistic use boundary", "all four disclosed source differences versus actual rule clauses", "all acceptance evidence ranges and checksums", "no copied physical addresses or deployment binding data"],
        "limits": ["offline artifact source audit only", "not a model extraction, publication, browser, retrieval recall, answer generation, or runtime diagnostic validation", "underlying business knowledge record was not provided and was not verified"],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "counts": report["counts"], "limits": report["limits"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
