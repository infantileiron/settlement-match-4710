import {ref} from 'vue';
import {request} from '../api/client';
import {fetchLedgers,createLedger,recalculateLedger,closeLedgerPeriod,fetchLedgerMonth} from '../api/ledgers';
import type {LedgerSummary,LedgerMonthView} from '../types/ledger';

const items=ref<LedgerSummary[]>([]);
const error=ref('');
const busy=ref(false);

async function run(task:()=>Promise<unknown>){
  busy.value=true;error.value='';
  try{await task()}catch(e){error.value=(e as Error).message}finally{busy.value=false}
}

export const ledgers={
  items,error,busy,
  async load(){await run(async()=>{items.value=await fetchLedgers()})},
  async create(body:unknown):Promise<LedgerSummary|undefined>{let created:LedgerSummary|undefined;await run(async()=>{created=await createLedger(body);items.value=await fetchLedgers()});return created},
  async recalculate(id:string,through:string|null){await run(async()=>{await recalculateLedger(id,through);items.value=await fetchLedgers()})},
  async close(id:string,period:string){await run(async()=>{await closeLedgerPeriod(id,period);items.value=await fetchLedgers()})},
  async month(id:string,period:string):Promise<LedgerMonthView|null>{try{return await fetchLedgerMonth(id,period)}catch(e){error.value=(e as Error).message;return null}},
  async batches(){return request('/batches')},
};
