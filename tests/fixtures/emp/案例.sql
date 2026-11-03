select s.id,
s.account_number,
s.is_datacom_flag,
s.cust_account_key,
s.create_by,
s.del_flag,
${p_cycle_id} as crt_cycle_id,
sysdate() as dw_last_update_date
from (
    select t1.account_id as id,
    t1.account_number,
    decode(t1.account_number,null,-99999,nvl(cust_d.cust_account_key,-99998)) as cust_account_key,
    b.is_datacom_flag,
    t1.create_by as create_by,
    case when t1.del_flag='N' and t1.delete_flag ='N' then 'N' else 'Y' end as del_flag,
    row_number() over (partition by t1.account_number order by t1.dw_last_update_date desc) rn 
    from sdi.eb_account_t t1
    left join (
        select tt.account_id,
        case when sum(tt.is_datacom_flag)>0 then 'Y'
        else 'N'
        end as is_datacom_flag
        from (
            select t2.account_id,
            case when t3.tag_id ='1122334455' then '1'
            else '0'
            end as is_datacom_flag
            from sdi.eb_account_tag_t t2 
            left join sdi.eb_account_common_tag_t t3 on t2.tag_id=t3.tag_id
            and t2.account_id is not null
            where t2.delete_flag='N'
         ) tt
         group by account_id
    ) b on t1.account_id=b.account_id
    left join dwrdim.customer_d cust_d
    on t1.account_number=cust_d.cust_account_number
    and cust_d.del_flag ='N'
    where 1=1 and b.is_datacom_flag is not null
) s where s.rn=1