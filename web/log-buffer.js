/* Bounded incoming history and an independent frozen viewing snapshot. */
class LogBuffer {
  constructor(capacity=20000){this.capacity=capacity;this.rows=[];this.cursor=0;this.follow=true;this.view=[];this.pending=0;this.generation=0;this.gap=false;}
  accept(batch,generation=this.generation){
    if(generation!==this.generation)return false;
    this.gap=this.gap||!!batch.truncated;
    const fresh=batch.items.filter(row=>row.id>this.cursor);
    this.rows=this.rows.concat(fresh).slice(-this.capacity);
    this.cursor=Math.max(this.cursor,batch.cursor);
    if(this.follow){this.view=this.rows;this.pending=0;}else this.pending+=fresh.length;
    return true;
  }
  pause(){this.follow=false;this.view=this.view.slice();}
  resume(){this.follow=true;this.view=this.rows;this.pending=0;}
  clear(cursor){this.generation++;this.cursor=cursor;this.rows=[];this.view=[];this.pending=0;this.gap=false;}
}
if(typeof module!=='undefined')module.exports=LogBuffer;
