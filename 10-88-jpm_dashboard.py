#!/usr/bin/env python3
"""
10-88: JPM Dashboard Aggregator
Aggregate analysis from 10-77 (Options structure), 10-73 (SI), 10-78 (PBD), 10-85 (Fintel), 10-323 (Options chains)
"""

import sys
import os
import json
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="JPM Dashboard - Aggregate technical analysis")
    parser.add_argument("--query", type=str, default="JPM", help="Ticker")
    parser.add_argument("--ticker", type=str, default=None, help="Ticker (compatibility)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--no-options", action="store_true", help="Skip options analysis")
    return parser.parse_args()

def run_script(script_name: str, ticker: str, json_output: bool = True) -> Optional[Dict]:
    """Run a 10-code script and return parsed JSON output."""
    script_paths = [
        Path.home() / script_name,
        Path.home() / f"{script_name}.py",
        Path.cwd() / script_name,
        Path.cwd() / f"{script_name}.py",
    ]
    
    script_path = None
    for p in script_paths:
        if p.exists():
            script_path = p
            break
    
    if not script_path:
        return None
    
    try:
        # 10-77 uses positional ticker argument, others use --query
        if "10-77" in script_name:
            cmd = [sys.executable, str(script_path), ticker]
        else:
            cmd = [sys.executable, str(script_path), "--query", ticker]
            if json_output:
                cmd.append("--json")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0 and json_output and result.stdout and "10-77" not in script_name:
            return json.loads(result.stdout)
        return {"raw_output": result.stdout, "error": result.stderr}
    except Exception as e:
        return {"error": str(e)}

def format_pbd_table(pbd_data: Dict) -> str:
    """Format PBD data as markdown table."""
    if not isinstance(pbd_data, dict):
        return "PBD data unavailable\n"
    
    lines = ["### PBD (10-78) Analysis\n"]
    lines.append("| TF | POC | VA | Setup | Bias |")
    lines.append("|-------|-------|-----------|---------|----------|")
    
    # Mock structure for demo (actual data would come from 10-78 output)
    for timeframe in ["1w", "1d", "4h"]:
        poc = pbd_data.get(f"{timeframe}_poc", "N/A")
        va = pbd_data.get(f"{timeframe}_va", "N/A-N/A")
        setup = pbd_data.get(f"{timeframe}_setup", "N/A")
        bias = pbd_data.get(f"{timeframe}_bias", "N/A")
        lines.append(f"| {timeframe} | {poc} | {va} | {setup} | {bias} |")
    
    return "\n".join(lines) + "\n"

def format_si_section(si_data: Dict) -> str:
    """Format short interest data from 10-73-v6 JSON."""
    if not si_data:
        return "### Short Interest (10-73)\nData unavailable\n"
    
    if isinstance(si_data, dict):
        si_pct = si_data.get("short_interest_pct")
        source = si_data.get("source", "unavailable")
        
        if si_pct is not None:
            si_str = f"{si_pct}%" if not str(si_pct).endswith('%') else si_pct
        else:
            si_str = "N/A"
        
        return f"""### Short Interest (10-73):
- SI: {si_str} (source: {source})
"""
    
    return "### Short Interest (10-73)\nData unavailable\n"

def format_fintel_section(fintel_data: Dict) -> str:
    """Format Fintel thesis."""
    if not isinstance(fintel_data, dict):
        return "Fintel Thesis: Data unavailable\n"
    
    si = fintel_data.get("short_interest_pct", "N/A")
    inst = fintel_data.get("institutional_delta_qtr", "N/A")
    signal = fintel_data.get("thesis", "N/A")
    score = fintel_data.get("signal_score", "N/A")
    
    return f"""### Fintel Thesis (10-85):
- Short % Float: {si if si != "N/A" else "N/A"}
- Inst Delta Qtr: {inst if inst != "N/A" else "N/A"}
- Signal: {signal} ({score}/1 score)
"""

def format_options_structure_section(raw_result: Dict) -> str:
    """Format 10-77 options structure (gamma walls, max pain, DTE) from raw output."""
    if not raw_result:
        return "### Options Structure (10-77)\nMarket closed or no chains available.\n"
    
    if "error" in raw_result and raw_result["error"]:
        return f"### Options Structure (10-77)\n[ERROR] {raw_result['error']}\n"
    
    if "raw_output" not in raw_result or not raw_result["raw_output"]:
        return "### Options Structure (10-77)\nMarket closed or no chains available.\n"
    
    options_output = raw_result["raw_output"]
    
    # Parse 10-77 output - look for the OPTIONS STRUCTURE section
    lines = options_output.split('\n')
    section_lines = []
    in_options_section = False
    
    for line in lines:
        # Start capturing at OPTIONS STRUCTURE header
        if '=== OPTIONS STRUCTURE' in line:
            in_options_section = True
        
        if in_options_section:
            section_lines.append(line)
            
            # Stop at risk warning
            if '[RISK WARNING]' in line:
                break
    
    # If we found the OPTIONS STRUCTURE section, format it nicely
    if section_lines and len(section_lines) > 1:
        output = "### Options Structure (10-77)\n"
        output += "\n".join(section_lines)
        return output + "\n"
    
    # If no OPTIONS STRUCTURE found, show what we got
    if options_output.strip():
        return f"### Options Structure (10-77)\n\n{options_output}\n"
    
    return "### Options Structure (10-77)\nMarket closed or no chains available.\n"

def main() -> int:
    args = parse_args()
    ticker = args.ticker or args.query
    
    timestamp = datetime.now(timezone.utc).strftime("%H:%M %Z")
    
    if args.json:
        # JSON output mode
        output = {
            "code": "10-88",
            "ticker": ticker,
            "timestamp": timestamp,
            "components": {
                "options_structure_10_77": run_script("10-77-options-engine", ticker, json_output=False),
                "pbd_10_78": run_script("10-78-pbd_analyzer-FIXED-v3-offline", ticker, json_output=True),
                "si_10_73": run_script("10-73", ticker, json_output=True),
                "fintel_10_85": run_script("10-85-fintel_thesis", ticker, json_output=True),
                "options_10_323": None if args.no_options else run_script("10-323", ticker, json_output=True),
            }
        }
        print(json.dumps(output, indent=2))
    else:
        # Text output mode (markdown format for dashboards)
        print(f"\n[JPM] 10-88 {ticker} Dashboard ({timestamp} EDT)\n")
        
        # Try to fetch component data
        options_77_result = run_script("10-77-options-engine", ticker, json_output=False)
        pbd_result = run_script("10-78-pbd_analyzer-FIXED-v3-offline", ticker, json_output=False)
        si_result = run_script("10-73-si-fetcher-v6", ticker, json_output=True)
        fintel_result = run_script("10-85-fintel_thesis", ticker, json_output=True)
        
        # Print OPTIONS STRUCTURE first (10-77 gamma/max pain/DTE)
        print(format_options_structure_section(options_77_result))
        
        # Print PBD analysis (10-78)
        if pbd_result and "raw_output" in pbd_result:
            print(pbd_result["raw_output"])
        else:
            print("### PBD (10-78) Analysis\n(Could not fetch - run with --json for details)\n")
        
        # Print Fintel (10-85)
        if fintel_result and isinstance(fintel_result, dict):
            print(format_fintel_section(fintel_result))
        
        # Print SI (10-73)
        if si_result and isinstance(si_result, dict):
            print(format_si_section(si_result))
        
        print("### Summary:\n")
        print("- **Overall**: Monitor gamma walls + max pain vs technical setup")
        print("- **Action**: DTE-aware entries near 1-sigma gamma levels, exits at max pain or breakouts")
        print("- **Next**: Update every 4h or on volume divergence.")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
