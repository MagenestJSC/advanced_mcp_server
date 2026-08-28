import { patch } from "@web/core/utils/patch";
import { ListRenderer } from "@web/views/list/list_renderer";

patch(ListRenderer.prototype, {
    setup() {
        super.setup(...arguments);
        this.showSecurityBanner = this.props.list.resModel === "adv.model.access";
    },
});
