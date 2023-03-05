import boto3
import botocore
# import jsonschema
import json
import traceback

from extutil import remove_none_attributes, account_context, ExtensionHandler, \
    ext, component_safe_name, current_epoch_time_usec_num, handle_common_errors

from botocore.exceptions import ClientError

eh = ExtensionHandler()

# {
#     'Name': 'zasdfasefd.cloudkommand.com.', 
#     'Type': 'A', 
#     'AliasTarget': {
#         'HostedZoneId': 'Z3AQBSTGFYJSTF', 
#         'DNSName': 's3-website-us-east-1.amazonaws.com.', 
#         'EvaluateTargetHealth': True
#         }
# }

route53 = boto3.client("route53")

def gen_s3_dns_value(region):
    if region.startswith("cn"):
        return f"s3-website.{region}.amazonaws.com.cn"
    else:
        return f"s3-website.{region}.amazonaws.com"

def lambda_handler(event, context):
    """This function will deploy a route53 record set for a given domain name

       It will auto-generate a domain name if you pass it a "base_domain". Use this to keep projects
       separated

       If you do not specify a hosted zone ID, it will choose the first one that matches
       the domain name and is public.
       
       If you specify a hosted zone ID, it will use that one.

       If you change the hosted zone ID, it will fail.
    """
    try:
        print(f"event = {event}")
        eh.capture_event(event)

        prev_state = event.get("prev_state") or {}
        cdef = event.get("component_def")
        cname = event.get("component_name")
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        domain = cdef.get("domain") or cdef.get("target_s3_bucket") or \
            form_domain(component_safe_name(project_code, repo_id, cname, no_underscores=False), cdef.get("base_domain"))
            # form_domain(, cdef.get("base_domain")) or \
        route53_hosted_zone_id = cdef.get("route53_hosted_zone_id")
        prior_route53_hosted_zone_id = prev_state.get("props", {}).get("route53_hosted_zone_id")
        if prior_route53_hosted_zone_id and route53_hosted_zone_id and (prior_route53_hosted_zone_id != route53_hosted_zone_id):
            eh.add_log("Cannot Change Hosted Zone ID", {"prior": prior_route53_hosted_zone_id, "new": route53_hosted_zone_id}, is_error=True)
            eh.perm_error("Cannot Change Hosted Zone ID", 0)
            return eh.finish()

        hosted_zone_id = prior_route53_hosted_zone_id or route53_hosted_zone_id

        record_type = cdef.get("record_type") or "A"

        pass_back_data = event.get("pass_back_data", {})
        if pass_back_data:
            pass
        else:
            eh.add_op("get_hosted_zone")
            eh.add_op("get_record_set")


        get_hosted_zone(hosted_zone_id, domain, record_type)
        get_record_set(prev_state, cdef, event.get("op"), domain, record_type)
        update_record_set(domain, record_type)
        check_update_complete()
        
        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()


@ext(handler=eh, op="get_hosted_zone")
def get_hosted_zone(hosted_zone_id, domain, record_type):
    if hosted_zone_id:
        try:
            old_zone = route53.get_hosted_zone(Id=hosted_zone_id).get("HostedZone")
            eh.add_log("Found Hosted Zone", {"response": old_zone})
            eh.add_props({"route53_hosted_zone_id": hosted_zone_id})
            # eh.add_op("get_record_set", {"zone": old_zone.get("HostedZone"), "domain": domain, "record_type": record_type})

        except ClientError as e:
            handle_common_errors(e, eh, "Failed to Get Hosted Zone", 10, ["NoSuchHostedZone", "InvalidInput"])
    
    else:
        hosted_zone_response = route53.list_hosted_zones()
        print(f"hosted_zone_response = {hosted_zone_response}")
        for zone in hosted_zone_response.get("HostedZones"):
            if zone.get("Config", {}).get("PrivateZone"):
                continue
            elif domain.endswith(zone['Name'][:-1]):
                old_zone = zone
                break

        print(f"old_zone = {old_zone}")
        if not old_zone and hosted_zone_response.get("IsTruncated"):
            marker = hosted_zone_response.get("NextMarker")
            while marker and not old_zone:
                hosted_zone_response = route53.list_hosted_zones(Marker=marker)
                for zone in hosted_zone_response.get("HostedZones"):
                    if zone.get("Config", {}).get("PrivateZone"):
                        continue
                    elif domain.endswith(zone['Name'][-1]):
                        old_zone = zone
                        break
                marker = hosted_zone_response.get("NextMarker") if hosted_zone_response.get("IsTruncated") else None

        if not old_zone:
            eh.add_log("No Hosted Zone Found", {"response": hosted_zone_response, "domain": domain})
            eh.perm_error("No Hosted Zone Found")
        else:
            eh.add_props({"route53_hosted_zone_id": old_zone.get("Id")})



@ext(handler=eh, op="get_record_set")
def get_record_set(prev_state, cdef, op, domain, record_type):
    # route53 = boto3.client("route53")
    # if not domain:
    #     eh.perm_error("No Domain")
    #     eh.add_log("No Domain or S3 Bucket", {"component_definition": cdef}, True)
    #     return 0

    route53_hosted_zone_id = eh.props["route53_hosted_zone_id"]

    response = route53.list_resource_record_sets(
        HostedZoneId=route53_hosted_zone_id,
        StartRecordName=domain
    )
    current_set = None
    print(f"Record Set Response {response}")
    for record_set in response.get("ResourceRecordSets"):
        if record_set.get("Name")[:-1] == domain and record_set.get("Type") == record_type:
            current_set = record_set
            current_set['Name'] = current_set['Name'][:-1]
            if current_set.get("AliasTarget").get("DNSName"):
                current_set['AliasTarget']['DNSName'] = current_set['AliasTarget']['DNSName'][:-1]
            eh.add_log("Found Matching Record Set", {"record_set": record_set})
            break

    if op == "delete":
        if current_set:
            eh.add_op("update_record_set", {"remove": current_set})
        else:
            eh.add_log("No Record Set to Delete", {"domain": domain, "record_type": record_type})

    else:
        identifier = cdef.get("identifier")
        weight = cdef.get("weight")
        region = cdef.get("region")
        continent = cdef.get("continent")
        state = cdef.get("state")
        country = cdef.get("country") if not state else "US"
        failover = cdef.get("failover")
        multivalue_answer = cdef.get("multivalue_answer")
        ttl = cdef.get("ttl")
        resource_records = cdef.get("resource_records")
        s3_region = cdef.get("target_s3_region")
        cloudfront_domain_name = cdef.get("target_cloudfront_domain_name")
        api_hosted_zone_id = cdef.get("target_api_hosted_zone_id")
        api_domain_name = cdef.get("target_api_domain_name")
        evaluate_health = cdef.get("evaluate_target_health") or False

        update_record_set_op_val = {}
        if api_hosted_zone_id and api_domain_name:
            hosted_zone_id = api_hosted_zone_id
            domain_name = api_domain_name
        elif cloudfront_domain_name:
            hosted_zone_id = "Z2FDTNDATAQYW2"
            domain_name = cloudfront_domain_name
        elif s3_region:
            hosted_zone_id = S3_MAPPING_DICT[s3_region]
            domain_name = f's3-website-{s3_region}.amazonaws.com.'
        else:
            eh.add_log("No API, Cloudfront, or S3 Data", {"definition": cdef}, is_error=True)
            eh.perm_error("No API, Cloudfront, or S3 Data", 0)
            return 0
        desired_set = remove_none_attributes({
            "Name": domain,
            "Type": record_type,
            "SetIdentifier": identifier,
            "Weight": weight,
            "Region": region,
            "GeoLocation": remove_none_attributes({
                "ContinentCode": continent,
                "CountryCode": country,
                "SubdivisionCode": state
            }) or None,
            "Failover": failover,
            "MultiValueAnswer": multivalue_answer,
            "TTL": ttl,
            "ResourceRecords": resource_records,
            "AliasTarget": remove_none_attributes({
                "HostedZoneId": hosted_zone_id,
                "DNSName": domain_name,
                "EvaluateTargetHealth": evaluate_health
            }) or None
        })

        eh.add_props({
            "hosted_zone_id": hosted_zone_id
        })

        print(f"current_set = {current_set}")
        print(f"desired_set = {desired_set}")
        if current_set != desired_set:
            update_record_set_op_val = {"upsert": desired_set}
        
        old_domain = prev_state.get("props", {}).get("domain")
        if old_domain and old_domain != domain:
            old_record_type = prev_state.get("props", {}).get("record_type")
            old_set = None
            response = route53.list_resource_record_sets(
                HostedZoneId=route53_hosted_zone_id,
                StartRecordName=old_domain
            )

            for record_set in response.get("ResourceRecordSets"):
                if record_set.get("Name")[:-1] == old_domain and record_set.get("Type") == old_record_type:
                    old_set = record_set
                    old_set['Name'] = old_set['Name'][:-1]
                    if old_set.get("AliasTarget").get("DNSName"):
                        old_set['AliasTarget']['DNSName'] = old_set['AliasTarget']['DNSName'][:-1]
                    eh.add_log("Found Set to Remove", {"record_set": record_set})
                    update_record_set_op_val["remove"] = old_set
                    break

        if update_record_set_op_val:
            eh.add_op("update_record_set", update_record_set_op_val)
        else:
            eh.add_log("No Records to Write", {"current_set": current_set})
            eh.add_props({"domain": domain, "hosted_zone_id": hosted_zone_id, "record_type": record_type})
            eh.add_links({
                "Record Set": gen_route53_link(hosted_zone_id),
                "Domain URL": f"https://{domain}"
            })

    # old_domain = prev_state.get("props", {}).get("domain") if op == "upsert" else domain
    # remove_set, remove_zone = None, None
    # if old_domain and ((old_domain != domain) or op == "delete"):
    #     remove_set, remove_zone = get_set(old_domain, record_type)

    # upsert_set, current_zone = None, None
    # if op == "upsert":
    #     current_set, current_zone = get_set(domain, record_type)
        
    #     else:
    #         eh.add_log("No Records to Write", {"current_set": current_set})
            

    # if remove_set or upsert_set:
    #     eh.add_op("update_record_set", {
    #         "remove": remove_none_attributes({"set": remove_set, "zone": remove_zone}), 
    #         "upsert": remove_none_attributes({"set": upsert_set, "zone": current_zone})
    #         })


@ext(handler=eh, op="update_record_set")
def update_record_set(domain, record_type):
    remove = eh.ops['update_record_set'].get("remove")
    upsert = eh.ops['update_record_set'].get("upsert")

    route53_hosted_zone_id = eh.props.get("route53_hosted_zone_id")

    # zone_1_id = upsert.get("zone", {}).get('Id')
    # zone_2_id = remove.get("zone", {}).get('Id')

    # if zone_1_id and zone_2_id and zone_1_id != zone_2_id:
    #     params1 = gen_params(zone_1_id, upsert.get("set"))
    #     change_id_1 = run_update(params1)
    #     params2 = gen_params(zone_2_id, remove.get("set"))
    #     change_id_2 = run_update(params2)
    #     eh.add_op("check_update_complete", [change_id_1, change_id_2])

    changes = []
    if upsert:
        changes.append({
            "Action": "UPSERT",
            "ResourceRecordSet": upsert
        })

    if remove:
        changes.append({
            "Action": "DELETE",
            "ResourceRecordSet": remove
        })

    params = {
        "HostedZoneId": route53_hosted_zone_id,
        "ChangeBatch": {
            "Comment":  "Change Made By CloudKommand",
            "Changes": changes
        }
    }
    try:
        response = route53.change_resource_record_sets(**params)
        eh.add_log("Updated Record(s)", {"response": response})
        change_id = response.get("ChangeInfo")['Id']
    except ClientError as e:
        handle_common_errors(e, eh, "Failed to Update Record", 60, ["NoSuchHostedZone", "NoSuchHealthCheck", "InvalidChangeBatch", "InvalidInput"])

    # change_id = run_update(params)
    eh.add_op("check_update_complete", [change_id])

    eh.add_props({"domain": domain, "record_type": record_type})
    eh.add_links({
        "Record Set": gen_route53_link(route53_hosted_zone_id),
        "Domain URL": f"https://{domain}"
    })


@ext(handler=eh, op="check_update_complete")
def check_update_complete():
    to_check = eh.ops['check_update_complete']
    for check_id in to_check:
        try:
            response = route53.get_change(Id=check_id)
            if response.get("ChangeInfo")['Status'] != "INSYNC":
                eh.add_log("Record Updating", {"id": check_id})
                eh.retry_error(str(current_epoch_time_usec_num()), progress=90, callback_sec=5)
                break
            # eh.add_log("Updated Record(s)", {"response": response})
        except ClientError as e:
            handle_common_errors(e, eh, "Failed to Check Status", 90, ["NoSuchChange", "InvalidInput"])

    if not eh.error:
        eh.add_log("Record(s) Complete", {"response": response})


def run_update(params):
    try:
        response = route53.change_resource_record_sets(**params)
        eh.add_log("Updated Record(s)", {"response": response})
        return response.get("ChangeInfo")['Id']
    except ClientError as e:
        handle_common_errors(e, eh, "Failed to Update Record", 60, ["NoSuchHostedZone", "NoSuchHealthCheck", "InvalidChangeBatch", "InvalidInput"])

    

def gen_params(zone_id, set_):
    return {
            "HostedZoneId": zone_id,
            "ChangeBatch": {
                "Comment":  "Change Made By CloudKommand",
                "Changes": [{
                    "Action": "UPSERT",
                    "ResourceRecordSet": set_
                }]
            }
        }

# def get_set(domain, record_type):
#     # route53 = boto3.client("route53")
#     found_set = None
#     old_zone = None
#     hosted_zone_response = route53.list_hosted_zones()
#     print(f"hosted_zone_response = {hosted_zone_response}")
#     for zone in hosted_zone_response.get("HostedZones"):
#         if domain.endswith(zone['Name'][:-1]):
#             old_zone = zone
#             break

#     print(f"old_zone = {old_zone}")
#     if not old_zone and hosted_zone_response.get("IsTruncated"):
#         marker = hosted_zone_response.get("NextMarker")
#         while marker and not old_zone:
#             hosted_zone_response = route53.list_hosted_zones(Marker=marker)
#             for zone in hosted_zone_response.get("HostedZones"):
#                 if domain.endswith(zone['Name'][-1]):
#                     old_zone = zone
#                     break
#             marker = hosted_zone_response.get("NextMarker") if hosted_zone_response.get("IsTruncated") else None
     
#     if old_zone:

        
#     if found_set:
#         found_set['Name'] = found_set['Name'][:-1]
#         if found_set.get("AliasTarget").get("DNSName"):
#             found_set['AliasTarget']['DNSName'] = found_set['AliasTarget']['DNSName'][:-1]
#         eh.add_log("Found Matching Record", {"set": found_set, "domain": domain})
#     else:
#         eh.add_log("No Matching Record", {"domain": domain})
    
#     return found_set, old_zone

def gen_route53_link(hosted_zone_id):
    return f"https://console.aws.amazon.com/route53/v2/hostedzones#ListRecordSets/{hosted_zone_id}"

def form_domain(bucket, base_domain):
    if bucket and base_domain:
        return f"{bucket}.{base_domain}"
    else:
        return None

S3_MAPPING_DICT = {
    "us-east-2": "Z2O1EMRO9K5GLX",
    "us-east-1": "Z3AQBSTGFYJSTF",
    "us-west-1": "Z2F56UZL2M1ACD",
    "us-west-2": "Z3BJ6K6RIION7M",
    "af-south-1": "Z83WF9RJE8B12",
    "ap-east-1": "ZNB98KWMFR0R6",
    "ap-south-1": "Z11RGJOFQNVJUP",
    "ap-northeast-3": "Z2YQB5RD63NC85",
    "ap-northeast-2": "Z3W03O7B5YMIYP",
    "ap-northeast-1": "Z2M4EHUR26P7ZW",
    "ap-southeast-2": "Z1WCIGYICN2BYD",
    "ap-southeast-1": "Z3O0J2DXBE1FTB",
    "ca-central-1": "Z1QDHH18159H29",
    "cn-northwest-1": "Z282HJ1KT0DH03",
    "eu-central-1": "Z21DNDUVLTQW6Q",
    "eu-west-1": "Z1BKCTXD74EZPE",
    "eu-west-2": "Z3GKZC51ZF0DB4",
    "eu-west-3": "Z3R1K369G5AVDG",
    "eu-south-1": "Z30OZKI7KPW7MI",
    "eu-north-1": "Z3BAZG2TWCNX0D",
    "me-south-1": "Z1MPMWCPA7YB62",
    "sa-east-1": "Z7KQH4QJS55SO"
}