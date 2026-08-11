import sys, io

src = open(sys.argv[1], 'r', encoding='utf-8', newline='').read()
lines = src.split('\n')

PVE_ANCHOR = '+m_PveEnabledPartitions=8'
SEC_HDR    = '[/Script/DuneSandbox.SecurityZonesSubsystem]'
PC_HDR     = '[/Script/DuneSandbox.DunePlayerCharacter]'

changes = []

# --- Edit 1: assert Habbanya (partition 1) as PvE, next to the existing PvE assert
if any(l.strip() == '+m_PveEnabledPartitions=1' for l in lines):
    changes.append('SKIP habbanya (already present)')
else:
    idx = next((i for i, l in enumerate(lines) if l.strip() == PVE_ANCHOR), None)
    if idx is None:
        sys.exit('FATAL: PvE anchor line not found')
    block = [
        '; Last Sietch 2026-08-03: assert Habbanya (partition 1, dim 0) as PvE EXPLICITLY rather than',
        '; leaning on the stock NullSec->Security PveFallback. Asserting beats defaulting: the',
        '; fallback is what keeps Habbanya safe today, and nothing here declared it on purpose.',
        '; PvP shards are 32 (Kulon, safe tradeposts) / 33 (Amtal, zones off) / 31 (DD).',
        '+m_PveEnabledPartitions=1',
    ]
    lines[idx+1:idx+1] = block
    changes.append('ADD +m_PveEnabledPartitions=1')

# --- Edit 2: pin m_bSecurityZonesForceEnablePvp=False explicitly
if any(l.strip().startswith('m_bSecurityZonesForceEnablePvp') for l in lines):
    changes.append('SKIP forceenablepvp (already present)')
else:
    idx = next((i for i, l in enumerate(lines) if l.strip() == SEC_HDR), None)
    if idx is None:
        sys.exit('FATAL: SecurityZonesSubsystem header not found')
    end = idx + 1
    while end < len(lines) and not lines[end].strip().startswith('['):
        end += 1
    while end > idx + 1 and lines[end-1].strip() == '':
        end -= 1
    block = [
        '; Last Sietch 2026-08-03: pin explicitly False. Funcom does NOT ship this key in DefaultGame.ini,',
        '; so it was running on an unread code default. True forces PvP INSIDE security-zone areas,',
        '; i.e. makes the sietch and tradeposts hostile: the exact inverse of the Kulon design.',
        '; Moot on Amtal (pod-33 disables security zones wholesale via -ini:game:).',
        'm_bSecurityZonesForceEnablePvp=False',
    ]
    lines[end:end] = block
    changes.append('ADD m_bSecurityZonesForceEnablePvp=False')

# --- Edit 3: raise the same-target repeated-kill cooldown (anti-spawncamp)
if any(l.strip().startswith('s_RepeatedKillCooldown') for l in lines):
    changes.append('SKIP repeatedkill (already present)')
else:
    if any(l.strip() == PC_HDR for l in lines):
        sys.exit('FATAL: DunePlayerCharacter section already exists; edit in place instead')
    while lines and lines[-1].strip() == '':
        lines.pop()
    block = [
        '',
        PC_HDR,
        '; Last Sietch 2026-08-03: anti-spawncamp. Stock is 300s. On Kulon/Amtal PvP is everywhere, so a',
        '; 5-minute same-target cooldown is thin; raised to 600s. Harmless on PvE Habbanya, which',
        '; has no PvP kills to gate. Shared PVC, so this applies battlegroup-wide by design.',
        's_RepeatedKillCooldown=600.0',
    ]
    lines.extend(block)
    changes.append('ADD s_RepeatedKillCooldown=600.0')

out = '\n'.join(lines)
if not out.endswith('\n'):
    out += '\n'
open(sys.argv[2], 'w', encoding='utf-8', newline='').write(out)
print('\n'.join(changes))
