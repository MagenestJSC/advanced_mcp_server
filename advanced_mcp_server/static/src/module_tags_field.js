/** @odoo-module **/

import { registry } from "@web/core/registry";
import {
    Many2ManyTagsField,
    many2ManyTagsField,
} from "@web/views/fields/many2many_tags/many2many_tags_field";

class ModuleTagsField extends Many2ManyTagsField {
    getTagProps(record) {
        const props = super.getTagProps(record);
        props.img = `/web/image/ir.module.module/${record.resId}/avatar_128`;
        return props;
    }
}

registry.category("fields").add("module_tags", {
    ...many2ManyTagsField,
    component: ModuleTagsField,
});
