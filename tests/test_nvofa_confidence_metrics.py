"""Ground truth distinguishes moving surfaces from newly revealed pixels."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from experiment_nvofa_confidence import regions, metrics, expand_cost, GW, GH


def main():
    truth, masks = regions((40, 42), (44, 42))
    assert np.all(truth[masks['moving']] == (-4, 0))
    assert not truth[masks['static']].any()
    assert masks['revealed'].sum() == 4*96
    assert not (masks['revealed'] & (masks['moving'] | masks['static'])).any()
    result = metrics(truth, truth, masks)
    assert result['moving_epe'] == result['static_epe'] == 0
    assert 'revealed_epe' not in result  # there is no visible correspondence
    cost = np.zeros((GH//4, GW//4), np.uint8)
    assert not expand_cost(cost).any()
    cost[10, 10] = 8
    expanded = expand_cost(cost)
    assert expanded[42, 42] == 8
    assert expanded[0, 0] == 0
    assert not (expanded > 8).any()  # strict greater-than, not >=
    assert (expanded > 7).any()
    print('PASS: backward-flow truth, disocclusion mask and four-tap cost rejection')


if __name__ == '__main__':
    main()
