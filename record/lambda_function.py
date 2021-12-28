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
    try:
        print(f"event = {event}")
        eh.capture_event(event)

        prev_state = event.get("prev_state") or {}
        cdef = event.get("component_def")
        cname = event.get("component_name")
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        domain = cdef.get("domain") or \
            form_domain(cdef.get("s3_bucket"), cdef.get("base_domain")) or \
            form_domain(component_safe_name(project_code, repo_id, cname, no_underscores=True), cdef.get("base_domain"))

        pass_back_data = event.get("pass_back_data", {})
        if not pass_back_data:
            eh.add_op("manage_record_set")

        manage_record_set(prev_state, cdef, event.get("op"), domain)
        update_record_set(domain)
        check_update_complete()
        
        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Uncovered Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()


@ext(handler=eh, op="manage_record_set")
def manage_record_set(prev_state, cdef, op, domain):
    # route53 = boto3.client("route53")
    record_type = cdef.get("record_type") or "A"
    if not domain:
        eh.perm_error("No Domain")
        eh.add_log("No Domain or S3 Bucket", {"component_definition": cdef}, True)
        return 0
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

    old_domain = prev_state.get("props", {}).get("domain") if op == "upsert" else domain
    remove_set = None
    if old_domain and ((old_domain != domain) or op == "delete"):
        remove_set, remove_zone = get_set(old_domain)

    upsert_set = None
    if op == "upsert":
        current_set, current_zone = get_set(domain)
        if api_hosted_zone_id and api_domain_name:
            hosted_zone_id = api_hosted_zone_id
            domain_name = api_domain_name
        elif cloudfront_domain_name:
            hosted_zone_id = "Z2FDTNDATAQYW2"
            domain_name = cloudfront_domain_name
        elif s3_region:
            hosted_zone_id = S3_MAPPING_DICT[s3_region]
            domain_name = f's3-website-{s3_region}.amazonaws.com.'
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

        if current_set != desired_set:
            upsert_set = desired_set
        else:
            eh.add_log("No Records to Write", {"current_set": current_set})
            eh.add_props({"domain": domain, "hosted_zone_id": current_zone['Id']})

    if remove_set or upsert_set:
        eh.add_op("update_record_set", {
            "remove": remove_none_attributes({"set": remove_set, "zone": remove_zone}), 
            "upsert": remove_none_attributes({"set": upsert_set, "zone": current_zone})
            })


@ext(handler=eh, op="update_record_set")
def update_record_set(domain):
    remove = eh.ops['update_record_set'].get("remove")
    upsert = eh.ops['update_record_set'].get("upsert")

    zone_1_id = upsert.get("zone", {}).get('Id')
    zone_2_id = remove.get("zone", {}).get('Id')

    if zone_1_id and zone_2_id and zone_1_id != zone_2_id:
        params1 = gen_params(zone_1_id, upsert.get("set"))
        change_id_1 = run_update(params1)
        params2 = gen_params(zone_2_id, remove.get("set"))
        change_id_2 = run_update(params2)
        eh.add_op("check_update_complete", [change_id_1, change_id_2])

    else:
        zone_id = zone_1_id or zone_2_id
        changes = []
        if zone_1_id:
            changes.append({
                "Action": "UPSERT",
                "ResourceRecordSet": upsert.get("set")
            })

        if zone_2_id:
            changes.append({
                "Action": "DELETE",
                "ResourceRecordSet": remove.get("set")
            })

        params = {
            "HostedZoneId": zone_id,
            "ChangeBatch": {
                "Comment":  "Change Made By CloudKommand",
                "Changes": changes
            }
        }
        change_id = run_update(params)
        eh.add_op("check_update_complete", [change_id])

    eh.add_props({"domain": domain, "hosted_zone_id": zone_1_id})


@ext(handler=eh, op="check_update_complete")
def check_update_complete():
    to_check = eh.ops['check_update_complete']
    for check_id in to_check:
        try:
            response = route53.get_change(Id=check_id)
            if response.get("ChangeInfo")['Status'] != "INSYNC":
                eh.retry_error(str(current_epoch_time_usec_num()), progress=90, callback_sec=4)
                break
            # eh.add_log("Updated Record(s)", {"response": response})
        except ClientError as e:
            handle_common_errors(e, eh, "Failed to Check Status", 90, ["NoSuchChange", "InvalidInput"])

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

def get_set(domain):
    # route53 = boto3.client("route53")
    found_set = None
    old_zone = None
    hosted_zone_response = route53.list_hosted_zones()
    for zone in hosted_zone_response.get("HostedZones"):
        if domain.endswith(zone['Name'][:-1]):
            old_zone = zone
            break

    if not old_zone and hosted_zone_response.get("IsTruncated"):
        marker = hosted_zone_response.get("NextMarker")
        while marker and not old_zone:
            hosted_zone_response = route53.list_hosted_zones(Marker=marker)
            for zone in hosted_zone_response.get("HostedZones"):
                if domain.endswith(zone['Name'][-1]):
                    old_zone = zone
                    break
            marker = hosted_zone_response.get("NextMarker") if hosted_zone_response.get("IsTruncated") else None
            
    if old_zone:

        response = route53.list_resource_record_sets(
            HostedZoneId=old_zone['Id'],
            StartRecordName=domain
            )

        for record_set in response.get("ResourceRecordSets"):
            if record_set.get("Name") == domain:
                found_set = record_set
                break
    if found_set:
        eh.add_log("Found Matching Record", {"set": found_set, "domain": domain})
    else:
        eh.add_log("No Matching Record", {"domain": domain})
    
    return found_set, old_zone

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