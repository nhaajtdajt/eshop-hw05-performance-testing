"""Generate reproducible JMeter plans and synthetic Flow 3 fixture data."""

import argparse
import csv
import datetime as dt
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
CAPACITY = 128
SCENARIOS = {
    "Load": ("SummaryReport", "Summary Report", [("steady", 8, 8, 65, 0, 0)]),
    "Stress": ("StatVisualizer", "Aggregate Report", [
        ("step_1", 8, 3, 20, 0, 0), ("step_2", 24, 3, 20, 0, 0),
        ("step_3", 48, 3, 20, 0, 0), ("step_4", 96, 3, 20, 0, 0),
    ]),
    "Spike": ("RespTimeGraphVisualizer", "Response Time Graph", [
        ("baseline", 8, 4, 60, 0, 0), ("burst", 56, 1, 12, 20, 16),
    ]),
    "Soak": (None, None, [("endurance", 8, 8, 630, 0, 0)]),
    "Smoke": (None, None, [("one_user", 1, 1, 12, 0, 0)]),
}


def prop(element, name, value, kind="stringProp"):
    node = ET.SubElement(element, kind, name=name)
    node.text = str(value).lower() if isinstance(value, bool) else str(value)
    return node


def component(tree, tag, gui, name, enabled=True):
    node = ET.SubElement(tree, tag, guiclass=gui, testclass=tag,
                         testname=name, enabled=str(enabled).lower())
    return node, ET.SubElement(tree, "hashTree")


def setting(key, default):
    return f"${{__P({key},{default})}}"


def headers(tree, authenticated=False):
    manager, _ = component(tree, "HeaderManager", "HeaderPanel", "JSON headers")
    fields = ET.SubElement(manager, "collectionProp", name="HeaderManager.headers")
    values = {"Content-Type": "application/json", "Accept": "application/json"}
    if authenticated:
        values["Authorization"] = "Bearer ${jwt}"
    for name, value in values.items():
        decl = ET.SubElement(fields, "elementProp", name="", elementType="Header")
        prop(decl, "Header.name", name)
        prop(decl, "Header.value", value)


def jsr(tree, suffix, label, code):
    node, _ = component(tree, f"JSR223{suffix}", "TestBeanGUI", label)
    for key, value in (("scriptLanguage", "groovy"), ("parameters", ""),
                       ("filename", ""), ("cacheKey", "true"), ("script", code)):
        prop(node, key, value)


INITIALIZE = """import org.apache.jmeter.services.FileServer
vars.put('flow_ok', 'true')
['resetToken', 'jwt', 'orderId', 'productName', 'productPrice', 'total'].each { vars.remove(it) }
if (vars.get('email') == null) {
    def base = new File(FileServer.getFileServer().getBaseDir())
    def csv = new File(base, '../data/test-users.csv')
    def lines = csv.readLines('UTF-8')
    int row = ctx.getThreadNum() + OFFSET + 1
    if (row >= lines.size()) throw new IllegalArgumentException('Insufficient fixture rows for this group')
    def columns = lines[0].split(',', -1)
    def values = lines[row].split(',', -1)
    if (columns.length != values.length) throw new IllegalArgumentException('Invalid fixture CSV row')
    columns.eachWithIndex { column, index -> vars.put(column, values[index]) }
}
"""

STEPS = [
    ("01_forgot_password", "POST", "/api/forgot-password",
     '{"email":"${email}"}', False,
     "assert data.resetToken?.toString() ==~ /[0-9]{4,6}/; vars.put('resetToken', data.resetToken.toString())"),
    ("02_reset_password", "POST", "/api/reset-password",
     '{"email":"${email}","resetToken":"${resetToken}","newPassword":"${newPassword}"}', False,
     "assert data.message == 'Password reset successfully'"),
    ("03_login", "POST", "/api/login", '{"email":"${email}","password":"${newPassword}"}', False,
     "assert data.user?.email == vars.get('email'); assert data.token; vars.put('jwt', data.token.toString())"),
    ("04_categories", "GET", "/api/categories", None, False,
     "assert data instanceof List; assert data.any { it.id?.toString() == vars.get('categoryId') }"),
    ("05_products", "GET", "/api/products", None, False,
     """assert data instanceof List
def product = data.find { it.id?.toString() == vars.get('productId') && it.category_id?.toString() == vars.get('categoryId') }
assert product?.name && product?.price instanceof Number
vars.put('productName', groovy.json.JsonOutput.toJson(product.name))
vars.put('productPrice', product.price.toString())
vars.put('total', (product.price * vars.get('quantity').toInteger()).toString())"""),
    ("06_add_to_cart", "POST", "/api/cart",
     '{"id":${productId},"name":${productName},"price":${productPrice},"quantity":${quantity}}', True,
     "assert data.message == 'Added to cart'"),
    ("07_checkout", "POST", "/api/checkout",
     '{"total_amount":${total},"shipping_address":"${shippingAddress}"}', True,
     "assert data.message == 'Checkout successful'; assert data.orderId instanceof Number && data.orderId > 0; vars.put('orderId', data.orderId.toString())"),
    ("08_cancel", "PUT", "/api/orders/${orderId}/cancel", None, True,
     "assert data.message == 'Order canceled successfully'"),
    ("09_verify_cancel", "GET", "/api/orders/${orderId}", None, True,
     "assert data.id?.toString() == vars.get('orderId'); assert data.status == 'canceled'; assert data.total_amount?.toString() == vars.get('total')"),
]


def write_step(tree, step, index, offset):
    label, method, path, body, auth, check = step
    if index:
        gate, tree = component(tree, "IfController", "IfControllerPanel", "Continue after successful step")
        prop(gate, "IfController.condition", "${flow_ok}")
        prop(gate, "IfController.evaluateAll", False, "boolProp")
        prop(gate, "IfController.useExpression", True, "boolProp")
    sampler, children = component(tree, "HTTPSamplerProxy", "HttpTestSampleGui", label)
    for name, value in {
        "domain": "127.0.0.1", "port": "3000", "protocol": "http", "path": path,
        "method": method, "connect_timeout": "2000", "response_timeout": "5000",
    }.items():
        prop(sampler, "HTTPSampler." + name, value)
    prop(sampler, "HTTPSampler.use_keepalive", True, "boolProp")
    prop(sampler, "HTTPSampler.postBodyRaw", body is not None, "boolProp")
    args = ET.SubElement(sampler, "elementProp", name="HTTPsampler.Arguments", elementType="Arguments")
    values = ET.SubElement(args, "collectionProp", name="Arguments.arguments")
    if body is not None:
        arg = ET.SubElement(values, "elementProp", name="", elementType="HTTPArgument")
        prop(arg, "HTTPArgument.always_encode", False, "boolProp")
        prop(arg, "Argument.value", body)
        prop(arg, "Argument.metadata", "=")
    if auth:
        headers(children, authenticated=True)
    if index == 0:
        jsr(children, "PreProcessor", "Pick CSV row once per virtual user",
            INITIALIZE.replace("OFFSET", str(offset)))
        timer, _ = component(children, "UniformRandomTimer", "UniformRandomTimerGui", "Pause before next workflow")
        prop(timer, "ConstantTimer.delay", setting("think_min_ms", 700))
        prop(timer, "RandomTimer.range", setting("think_jitter_ms", 600))
    check_code = """import groovy.json.JsonSlurper
try {
    assert prev.getResponseCode() == '200'
    def data = new JsonSlurper().parseText(prev.getResponseDataAsString())
    EXPECTED
} catch (AssertionError | Exception failure) {
    vars.put('flow_ok', 'false')
    AssertionResult.setFailure(true)
    AssertionResult.setFailureMessage('Flow step failed: ' + prev.getSampleLabel() + ' (HTTP ' + prev.getResponseCode() + ')')
}
"""
    jsr(children, "Assertion", "Validate response and pass workflow data", check_code.replace("EXPECTED", check))


def controller(node, loops):
    prop(node, "LoopController.continue_forever", False, "boolProp")
    prop(node, "LoopController.loops", loops)


def add_group(tree, stage, users, ramp, duration, delay, offset, once):
    group, children = component(tree, "ThreadGroup", "ThreadGroupGui", stage)
    prop(group, "ThreadGroup.on_sample_error", "continue")
    main = ET.SubElement(group, "elementProp", name="ThreadGroup.main_controller", elementType="LoopController",
                         guiclass="LoopControlPanel", testclass="LoopController", testname="Workflow loop")
    controller(main, 1 if once else -1)
    for name, value in {"num_threads": setting(stage + "_users", users),
                        "ramp_time": setting(stage + "_ramp", ramp),
                        "duration": setting(stage + "_seconds", duration),
                        "delay": setting(stage + "_delay", delay)}.items():
        prop(group, "ThreadGroup." + name, value)
    prop(group, "ThreadGroup.scheduler", True, "boolProp")
    prop(group, "ThreadGroup.same_user_on_next_iteration", True, "boolProp")

    transaction, requests = component(children, "TransactionController", "TransactionControllerGui", "flow_03")
    prop(transaction, "TransactionController.parent", False, "boolProp")
    prop(transaction, "TransactionController.includeTimers", False, "boolProp")
    for index, step in enumerate(STEPS):
        write_step(requests, step, index, offset)


def write_plan(target, scenario):
    gui, view, groups = SCENARIOS[scenario]
    root = ET.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter="5.6.3")
    plan, tree = component(ET.SubElement(root, "hashTree"), "TestPlan", "TestPlanGui", target.stem)
    prop(plan, "TestPlan.comments", "Flow 3 on the local EShop fixture. Reopen each scenario's CSV JTL in its report view.")
    prop(plan, "TestPlan.serialize_threadgroups", scenario == "Stress", "boolProp")
    prop(plan, "TestPlan.functional_mode", False, "boolProp")
    variables = ET.SubElement(plan, "elementProp", name="TestPlan.user_defined_variables", elementType="Arguments",
                              guiclass="ArgumentsPanel", testclass="Arguments", testname="Variables")
    ET.SubElement(variables, "collectionProp", name="Arguments.arguments")
    headers(tree)
    for group in groups:
        add_group(tree, *group, once=scenario == "Smoke")
    if gui:
        listener, _ = component(tree, "ResultCollector", gui, view, enabled=False)
        prop(listener, "ResultCollector.error_logging", False, "boolProp")
        prop(listener, "filename", "")
        obj = ET.SubElement(listener, "objProp")
        ET.SubElement(obj, "name").text = "saveConfig"
        config = ET.SubElement(obj, "value", {"class": "SampleSaveConfiguration"})
        for key in ("time", "latency", "timestamp", "success", "label", "code", "message", "threadName",
                    "assertions", "subresults", "fieldNames", "bytes", "sentBytes", "threadCounts", "connectTime"):
            ET.SubElement(config, key).text = "true"
    ET.indent(root)
    ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-id", default="23120231")
    parser.add_argument("--date", default=dt.date.today().strftime("%Y%m%d"))
    args = parser.parse_args()
    for folder in ("plans", "data"):
        (ROOT / folder).mkdir(exist_ok=True)
    with (ROOT / "data" / "test-users.csv").open("w", newline="", encoding="utf-8") as file:
        csvfile = csv.writer(file, lineterminator="\n")
        csvfile.writerow(("email", "newPassword", "productId", "categoryId", "quantity", "shippingAddress"))
        for number in range(CAPACITY):
            product_id = number % 5 + 1
            csvfile.writerow((f"flow3-{number + 1:03d}@example.test", f"Flow3Run{number + 1:03d}!",
                              product_id, [1, 1, 2, 3, 3][product_id - 1], 1, "HW05 synthetic address"))
    for scenario in SCENARIOS:
        folder = ROOT / ("plans" if scenario in ("Load", "Stress", "Spike") else ".runtime")
        folder.mkdir(exist_ok=True)
        write_plan(folder / f"{args.student_id}_{scenario}_{args.date}.jmx", scenario)


if __name__ == "__main__":
    main()
