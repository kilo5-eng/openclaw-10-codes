#!/bin/bash
# Code Integrity Guard: Prevent script drift and unintended modifications
# Run this before making changes to critical 10-code scripts

set -e

WORKSPACE="/home/kcinc/.openclaw/workspace"
HERMES_SCRIPTS="$WORKSPACE/hermes-config/10-codes/scripts"
OPENCLAW_SCRIPTS="$WORKSPACE/scripts"

# Define canonical script locations (source of truth)
declare -A CANONICAL_SCRIPTS=(
  ["10_073_short_interest_compare.py"]="$HERMES_SCRIPTS/10_073_short_interest_compare.py"
  ["10_011_research_intel.py"]="$HERMES_SCRIPTS/10_011_research_intel.py"
  ["10_323_csp_call_strategy.py"]="$HERMES_SCRIPTS/10_323_csp_call_strategy.py"
)

echo "=== CODE INTEGRITY CHECK ==="
echo "Workspace: $WORKSPACE"
echo

VIOLATIONS=0

for script_name in "${!CANONICAL_SCRIPTS[@]}"; do
  canonical_path="${CANONICAL_SCRIPTS[$script_name]}"
  
  # Check if canonical exists
  if [ ! -f "$canonical_path" ]; then
    echo "❌ MISSING: $script_name at $canonical_path"
    ((VIOLATIONS++))
    continue
  fi
  
  # Check if duplicate exists in openclaw/scripts/
  dup_path="$OPENCLAW_SCRIPTS/${script_name%.py}.sh"
  if [ -f "$dup_path" ]; then
    echo "⚠️  DUPLICATE DETECTED: $script_name"
    echo "   Canonical: $canonical_path"
    echo "   Duplicate: $dup_path"
    echo "   Action: Use canonical only; remove duplicates"
    ((VIOLATIONS++))
  fi
done

if [ $VIOLATIONS -eq 0 ]; then
  echo "✅ All scripts canonical and in correct locations"
  exit 0
else
  echo
  echo "❌ $VIOLATIONS violation(s) detected"
  echo "Fix: Only modify/run scripts from hermes-config/10-codes/scripts/"
  exit 1
fi
