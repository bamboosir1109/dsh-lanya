import importlib.util
import json
from pathlib import Path
import unittest

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('interaction_health',REPO/'scripts/interaction_health.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class InteractionHealthTests(unittest.TestCase):
    def test_listening_is_terminal_and_buffer_metadata_does_not_hide_real_pending_work(self):
        def row(i,phase):
            return dict(schema='qq-interaction/v1',scope='c'*16,input=f'{i:024x}',phase=phase,at=1000)
        rows=[row(1,'received'),row(1,'group_listening'),row(2,'queued'),row(2,'group_buffer_wait'),
              row(3,'queued'),row(3,'group_buffer_ready'),row(3,'claimed')]
        value=module.summarize('\n'.join(json.dumps(x) for x in rows).encode(),40000)
        self.assertEqual(value['status'],'ok');self.assertEqual(value['invalid_records'],0)
        self.assertEqual(value['scopes'][0]['counts'],{'group_listening':1,'pending':1,'active':1})
        self.assertEqual(value['scopes'][0]['long_open_inputs'],2)

    def test_transport_acceptance_quiet_pending_and_unknown_remain_distinct(self):
        def row(i,phase,at,**fields):
            return dict(schema='qq-interaction/v1',scope='a'*16,input=f'{i:024x}',phase=phase,at=at,**fields)
        rows=[row(1,'received',1000),row(1,'delivery',1300,status='sent'),
              row(2,'received',1100),row(2,'decision',1400,verdict='idle'),row(2,'turn_end',1500),
              row(3,'received',1200),row(3,'delivery',1600,status='unknown'),row(4,'queued',2000)]
        value=module.summarize('\n'.join(json.dumps(x) for x in rows).encode(),40000)
        group=value['scopes'][0]
        self.assertEqual(group['counts'],{'transport_accepted':1,'decided_quiet':1,'delivery_uncertain':1,'pending':1})
        self.assertEqual(group['accepted_p50_ms'],300)
        self.assertEqual(group['long_open_inputs'],1)
        self.assertEqual(value['receipt_boundary'],'transport_acceptance_only')
        self.assertFalse(value['history_replayed'])

    def test_partial_or_invalid_source_is_not_reported_as_a_healthy_empty_history(self):
        value=module.summarize(b'{"unfinished":\n',40000)
        self.assertEqual(value['status'],'partial');self.assertEqual(value['invalid_records'],1)
        with self.assertRaises(ValueError):module.summarize(b' '*(module.LIMIT+1),40000)

    def test_merged_input_is_not_a_stuck_queue_and_rejected_silence_is_not_a_quiet_decision(self):
        def row(i,phase,**fields):
            return dict(schema='qq-interaction/v1',scope='b'*16,input=f'{i:024x}',phase=phase,at=1000,**fields)
        rows=[row(1,'queued',coalesced=False),row(2,'queued',coalesced=True),row(2,'claimed'),
              row(2,'decision',verdict='idle'),row(2,'turn_end'),
              row(3,'decision',verdict='silence',reason='unauthorized_control'),row(3,'turn_end'),
              row(4,'delivery',status='sent'),row(4,'delivery_refused',reason='stale_revision')]
        value=module.summarize('\n'.join(json.dumps(x) for x in rows).encode(),40000)
        self.assertEqual(value['scopes'][0]['counts'],{'coalesced':1,'decided_quiet':1,
            'ended_without_confirmed_transport':1,'partial_transport':1})
        self.assertEqual(value['scopes'][0]['long_open_inputs'],0)
        missing=module.summarize(json.dumps(row(5,'queued',coalesced=True)).encode(),40000)
        self.assertEqual(missing['status'],'partial');self.assertEqual(missing['missing_coalesced_parents'],1)
        duplicate=[row(6,'admitted'),row(6,'claimed'),row(6,'queued'),row(6,'not_admitted')]
        value=module.summarize('\n'.join(json.dumps(x) for x in duplicate).encode(),40000)
        self.assertEqual(value['scopes'][0]['counts'],{'active':1})


if __name__=='__main__':unittest.main()
