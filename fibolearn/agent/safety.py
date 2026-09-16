from __future__ import annotations
class ReadOnlyTradeGuard:
    BLOCKED={'place_order','cancel_order','modify_order','close_position','start_goldenfibo_trade','change_goldenfibo_parameters','set_sl','set_tp'}
    def execute(self, request):
        op=str((request or {}).get('operation','')).lower()
        if op in self.BLOCKED:
            raise PermissionError(f'FiboLearn Phase 1 is read-only; blocked trading operation: {op}')
        return {'allowed': True, 'operation': op}
