import {request} from "./client";
export const fetchLedgers=()=>request("/ledgers");
export const createLedger=(body:unknown)=>request("/ledgers","POST",body);
export const recalculateLedger=(id:string,through_period:string|null)=>request("/ledgers/"+id+"/recalculate","POST",{through_period});
export const closeLedgerPeriod=(id:string,period:string)=>request("/ledgers/"+id+"/close","POST",{period});
export const fetchLedgerMonth=(id:string,period:string)=>request("/ledgers/"+id+"/months/"+period);
export const deleteLedger=(id:string)=>request("/ledgers/"+id,"DELETE");
