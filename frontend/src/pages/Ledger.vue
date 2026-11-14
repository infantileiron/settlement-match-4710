<script setup lang="ts">
import {computed,onMounted,ref} from 'vue';
import {request} from '../api/client';
import {fetchLedgerMonth} from '../api/ledgers';
import {ledgers as store} from '../stores/ledgers';
import type {LedgerSummary,LedgerMonthView} from '../types/ledger';
import {canWrite,isAdmin} from '../stores/auth';
import {money} from '../utils/format';
import {useTask} from '../hooks/useTask';
import PageHeading from '../components/common/PageHeading.vue';
import Notice from '../components/common/Notice.vue';
import EmptyState from '../components/common/EmptyState.vue';

const ledgers=store.items;
const selected=ref('');
const monthView=ref<LedgerMonthView|null>(null);
const expanded=ref('');
const batches=ref<any[]>([]);
const name=ref('跨期余额台账');
const batch=ref('demo-batch');
const through=ref('');
const createThrough=ref('');
const {error,busy,act}=useTask();

const current=computed(()=>ledgers.value.find(r=>r.id===selected.value)||null);
const firstOpen=computed(()=>current.value?.periods.find(p=>p.status==='open')||null);
const outstandingTotal=computed(()=>current.value?.outstanding_commissions.reduce((sum,r:any)=>sum+r.closing_cents,0)||0);
const sourceLabels:Record<string,string>={normal:'当期',carried_receipt:'结转凭证余款',backfill_receipt:'前期补记凭证',backfill_commission:'前期补记委托'};
const reasonLabels:Record<string,string>={backdated_receipt:'前期补记到账',new_commission:'关账后新增委托',recomputed_shift:'重算推移'};
const statusLabels:Record<string,string>={open:'开放',closed:'已关账',matched:'已对平',unallocated:'凭证余款'};

async function load(keep?:string){
  await store.load();
  if(keep&&ledgers.value.some(r=>r.id===keep))selected.value=keep;
  if(!selected.value&&ledgers.value.length)selected.value=ledgers.value[0].id;
  const row=current.value;
  if(row){through.value=row.through_period;await openMonth(row.periods[row.periods.length-1]?.period||'')}
}
async function openMonth(period:string){
  monthView.value=null;expanded.value='';
  if(!selected.value||!period)return;
  monthView.value=await store.month(selected.value,period);
}
async function selectLedger(id:string){
  selected.value=id;const row=ledgers.value.find(r=>r.id===id);
  through.value=row?.through_period||'';
  await openMonth(row?.periods[row.periods.length-1]?.period||'');
}
function submit(){
  act(async()=>{
    const created=await store.create({batch_id:batch.value,name:name.value,description:'逐笔委托与凭证抵扣，跨月结转未清余额',through_period:createThrough.value||null});
    if(created)await load(created.id);
  });
}
function recalculate(){
  act(async()=>{await store.recalculate(selected.value,through.value||null);await load(selected.value)});
}
function closePeriod(period:string){
  act(async()=>{await store.close(selected.value,period);await load(selected.value)});
}
onMounted(async()=>{
  const rows:any[]=await request('/batches');
  batches.value=rows;
  if(rows.length&&!rows.some(r=>r.id===batch.value))batch.value=rows[0].id;
  act(load);
});
</script>

<template>
<PageHeading index="03 / 跨期余额" title="每一分欠款，都能沿月份回查到凭证。" note="按委托与凭证逐次抵扣：委托原额 = 累计抵扣 + 未清余额，凭证原额 = 已抵扣 + 余款。结转只带未清部分，重复执行不新增余额；已关账月份保留原账并单列重算差额。"/>
<Notice :message="error"/>
<div class="two-col">
<section>
  <div class="item-controls">
    <label>余额台账<select :value="selected" @change="selectLedger(($event.target as HTMLSelectElement).value)"><option v-for="r in ledgers" :key="r.id" :value="r.id">{{r.name}}</option></select></label>
    <label>账期至<input v-model="through" placeholder="YYYY-MM"></label>
  </div>
  <div class="actions" v-if="current">
    <button v-if="canWrite()" :disabled="busy" @click="recalculate">重算台账</button>
    <button v-if="isAdmin()&&firstOpen" class="secondary" :disabled="busy" @click="closePeriod(firstOpen.period)">关账 {{firstOpen.period}}</button>
  </div>
  <template v-if="current">
    <div class="match-metrics">
      <div><small>委托原额</small><strong>{{money(current.checks.commissions?.original_cents)}}</strong><span>累计抵扣 {{money(current.checks.commissions?.allocated_cents)}} 元</span></div>
      <div><small>未清余额</small><strong>{{money(outstandingTotal)}}</strong><span>{{current.outstanding_commissions.length}} 笔委托跨期结转</span></div>
      <div><small>凭证对平</small><strong>{{money(current.checks.receipts?.allocated_cents)}} <em>/ {{money(current.checks.receipts?.amount_cents)}}</em></strong><span>余款 {{money(current.checks.receipts?.remaining_cents)}} 元 · {{current.checks.receipts?.matched}} 笔对平</span></div>
    </div>
    <p class="method-note" v-if="current.checks.commissions?.closed_cross_foot_cents">已关账账册的余额与抵扣明细相差 {{money(current.checks.commissions.closed_cross_foot_cents)}} 元；台账以已关账余额为准，差额见下方“已关账月份的差额”。</p>
    <p class="method-note" v-if="current.closed_through">已关账至 {{current.closed_through}}；开放账期从 {{current.periods.find(p=>p.status==='open')?.period||'—'}} 起。过期月份不回写，只在下方列示差额。</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>月份</th><th>状态</th><th>期初欠款元</th><th>当月新增元</th><th>当月抵扣元</th><th>期末欠款元</th><th>委托数</th><th>抵扣笔数</th></tr></thead>
        <tbody>
          <tr v-for="p in current.periods" :key="p.period" :class="{active:p.period===monthView?.period.period}" @click="openMonth(p.period)">
            <td><strong>{{p.period}}</strong></td>
            <td>{{statusLabels[p.status]||p.status}}</td>
            <td>{{money(p.opening_cents)}}</td>
            <td>{{money(p.new_cents)}}</td>
            <td>{{money(p.applied_cents)}}</td>
            <td>{{money(p.closing_cents)}}</td>
            <td>{{p.commission_count}}</td>
            <td>{{p.allocation_count}}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="method-note">点击月份查看该月每笔委托余额与逐次抵扣来源。</p>
    <template v-if="monthView">
      <h2 class="section-space">{{monthView.period.period}} 余额明细</h2>
      <p class="method-note">期初 {{money(monthView.period.opening_cents)}} + 新增 {{money(monthView.period.new_cents)}} − 抵扣 {{money(monthView.period.applied_cents)}} = 期末 {{money(monthView.period.closing_cents)}} 元；{{monthView.period.status==='closed'?'该月已关账，数据来自冻结账。':'该月开放，可随补记重算。'}}</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>委托ID</th><th>往来方 / 引用</th><th>原额元</th><th>期初元</th><th>新增元</th><th>当月抵扣元</th><th>期末元</th><th>累计抵扣元</th><th>等式</th></tr></thead>
          <tbody>
            <template v-for="row in monthView.balances" :key="row.commission_external_id">
              <tr @click="expanded=expanded===row.commission_external_id?'':row.commission_external_id">
                <td>{{row.commission_external_id}}<small v-if="row.carried_from_period" class="row-note">由 {{row.carried_from_period}} 结转未清</small></td>
                <td>{{row.party}}<small class="row-note">{{row.reference||'—'}}</small></td>
                <td>{{money(row.original_cents)}}</td>
                <td>{{money(row.opening_cents)}}</td>
                <td>{{money(row.new_cents)}}</td>
                <td>{{money(row.applied_cents)}}</td>
                <td>{{money(row.closing_cents)}}</td>
                <td>{{money(row.applied_to_date_cents)}}</td>
                <td>{{row.equation_ok?'对平':'有差额'}}</td>
              </tr>
              <tr v-if="expanded===row.commission_external_id">
                <td colspan="9">
                  <p class="method-note">{{row.commission_external_id}}：原额 {{money(row.original_cents)}} 元 = 累计抵扣 {{money(row.applied_to_date_cents)}} 元 + 未清 {{money(row.closing_cents)}} 元。</p>
                  <div class="table-wrap">
                    <table>
                      <thead><tr><th>抵扣月份</th><th>凭证</th><th>入账日期</th><th>本次抵扣元</th><th>来源</th><th>凭证状态</th></tr></thead>
                      <tbody>
                        <tr v-for="a in row.prior_allocations" :key="a.period+a.receipt_external_id">
                          <td>{{a.period}}</td><td>{{a.receipt_external_id}}</td><td>—</td><td>{{money(a.amount_cents)}}</td><td>{{sourceLabels[a.source]||a.source}}</td><td>历史期</td>
                        </tr>
                        <tr v-for="a in row.allocations" :key="a.sequence+a.receipt_external_id">
                          <td>{{monthView.period.period}}</td><td>{{a.receipt_external_id}}</td><td>{{a.receipt_booking_date}}</td><td>{{money(a.amount_cents)}}</td><td>{{sourceLabels[a.source]||a.source}}</td><td>{{statusLabels[a.receipt_status]||a.receipt_status}}</td>
                        </tr>
                        <tr v-if="!row.allocations.length&&!row.prior_allocations.length"><td colspan="6">本月无抵扣，仅结转未清余额。</td></tr>
                      </tbody>
                    </table>
                  </div>
                </td>
              </tr>
            </template>
            <tr v-if="!monthView.balances.length"><td colspan="9">该月没有在册委托余额。</td></tr>
          </tbody>
        </table>
      </div>
    </template>
    <h2 class="section-space">凭证逐分核对</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>凭证</th><th>往来方 / 引用</th><th>入账日期</th><th>入账元</th><th>已抵扣元</th><th>余款元</th><th>状态</th></tr></thead>
        <tbody><tr v-for="r in current.receipts" :key="r.receipt_external_id"><td>{{r.receipt_external_id}}</td><td>{{r.party}}<small class="row-note">{{r.reference||'—'}}</small></td><td>{{r.booking_date}}</td><td>{{money(r.amount_cents)}}</td><td>{{money(r.allocated_cents)}}</td><td>{{money(r.remaining_cents)}}</td><td>{{statusLabels[r.status]||r.status}}</td></tr></tbody>
      </table>
    </div>
    <template v-if="current.variances.length">
      <h2 class="section-space">已关账月份的差额</h2>
      <p class="method-note">关账后发生前期补记时，不回写原账；以下为重算值与保留值的逐项差额。</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>月份</th><th>对象</th><th>保留元</th><th>重算元</th><th>差额元</th><th>原因</th></tr></thead>
          <tbody><tr v-for="v in current.variances" :key="v.period+v.entity_type+v.entity_id"><td>{{v.period}}</td><td>{{v.entity_type==='commission'?'委托':'凭证'}} {{v.entity_id}}</td><td>{{money(v.kept_cents)}}</td><td>{{money(v.recomputed_cents)}}</td><td>{{money(v.delta_cents)}}</td><td>{{reasonLabels[v.reason]||v.reason}}</td></tr></tbody>
        </table>
      </div>
    </template>
    <p class="hash-note" v-if="current.recalculated_at">最近重算 {{current.recalculated_at}} · 创建人 {{current.created_by}}</p>
  </template>
  <EmptyState v-else text="还没有余额台账。选择批次并建立台账后，系统按逐次抵扣生成跨月余额。"/>
</section>
<form v-if="canWrite()" class="editor-panel" @submit.prevent="submit">
  <h2>建立余额台账</h2>
  <label>对账批次<select v-model="batch"><option v-for="r in batches" :key="r.id" :value="r.id">{{r.name}}</option></select></label>
  <label>台账名称<input v-model="name" required minlength="2" maxlength="180"></label>
  <label>账期至（可留空）<input v-model="createThrough" placeholder="YYYY-MM"></label>
  <p class="method-note">按委托的往来方 + 引用 + 币种硬关联凭证；同一委托的多笔到账逐次抵扣，先到期先抵，结转只带未清余额。补录分录会自动重算开放账期。</p>
  <button :disabled="busy">建立并重算台账</button>
</form>
</div>
</template>
